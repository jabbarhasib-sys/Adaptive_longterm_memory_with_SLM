#!/usr/bin/env python3
"""
Train/Test Evaluation Pipeline for Adaptive Long-Term Memory (team_flat strategy)
===================================================================================

Split:
  - Train (calibration): First 470 chats from longmemeval_s.json
    * Grid-searches over scoring weights, admission thresholds, and budget
    * Picks the parameter set that maximises Recall@K on the train split
  - Test  (held-out):    Last 30 chats from longmemeval_s.json
    * Evaluates the best parameters found above on unseen examples
    * Reports Accuracy, Precision@K, Recall@K, Token-F1, MRR, Stale Error Rate

Usage:
    python train_test_eval.py [--budget 500] [--top-k 5] [--limit-train N] [--skip-train]
                              [--output-dir outputs/train_test]

Author: Jabbar Hasib / Teammate A (Flat Memory Strategy)
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from candidate_generation import CandidateAdmissionGate
from evaluation_harness import (
    evaluate_answer_accuracy,
    evaluate_retrieval_quality,
    normalize_text,
)
from memory_core import ScoringWeights
from memory_retrieval import retrieve_memories
from model_adapter import FakeLLMAdapter, heuristic_bpe_token_count
from strategies.flat_team import TeamFlatMemoryStrategy


# ============================================================================
# 1. Dataset helpers
# ============================================================================

_DATE_RE = re.compile(r"\s*\([A-Za-z]+\)\s*")


def _parse_date(date_str: str, fallback: float = 0.0) -> float:
    cleaned = _DATE_RE.sub(" ", date_str).strip()
    try:
        return datetime.datetime.strptime(cleaned, "%Y/%m/%d %H:%M").timestamp()
    except ValueError:
        return fallback


def _load_longmemeval(
    path: str,
    train_n: int = 470,
    test_n: int = 30,
) -> Tuple[List[Dict], List[Dict]]:
    """Load raw longmemeval_s.json and return (train_items, test_items)."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    total = len(data)
    print(f"[data] Loaded {total} items from {path}")
    if total < train_n + test_n:
        raise ValueError(
            f"Dataset has only {total} items; need at least {train_n + test_n}."
        )
    # First train_n for calibration, last test_n for held-out test (no overlap)
    train = data[:train_n]
    test = data[total - test_n:]
    print(f"[data] Train: {len(train)}  |  Test: {len(test)}")
    return train, test


_STOPWORDS = {
    "a", "an", "the", "is", "are", "was", "were", "in", "on", "at", "to", "for",
    "of", "and", "or", "you", "i", "my", "your", "it", "that", "this", "me", "we",
    "our", "what", "can", "how", "do", "did", "so", "be", "with", "from", "as", "by",
    "have", "has", "had", "not", "but",
}


def _item_to_sessions(item: Dict) -> Tuple[List[Tuple[str, List[Dict]]], List[str]]:
    """
    Convert a raw longmemeval_s item into:
      - sessions: list of (session_id, list_of_turns)
      - supporting_evidence: list of turn content strings that contain the answer
    Skips empty sessions. Assigns timestamps from haystack_dates.
    """
    sessions: List[Tuple[str, List[Dict]]] = []
    supporting_evidence: List[str] = []

    sess_ids = item["haystack_session_ids"]
    raw_sessions = item["haystack_sessions"]
    dates = item["haystack_dates"]
    answer_sids = set(item.get("answer_session_ids", []))
    answer_str = str(item.get("answer", "")).strip()
    ans_norm = normalize_text(answer_str)
    a_words = set(ans_norm.split()) - _STOPWORDS
    q_words = set(normalize_text(item.get("question", "")).split()) - _STOPWORDS
    target_words = q_words | a_words

    candidate_evidence: List[str] = []

    for idx, (sid, sess, date_str) in enumerate(zip(sess_ids, raw_sessions, dates)):
        if not sess:
            continue
        base_ts = _parse_date(date_str, fallback=1_684_529_460.0 + idx * 3600.0)
        turns = []
        for t_idx, turn in enumerate(sess):
            content = turn.get("content", "").strip()
            if not content:
                continue
            turns.append(
                {
                    "role": turn.get("role", "user"),
                    "content": content,
                    "timestamp": base_ts + t_idx * 10.0,
                }
            )
            if sid in answer_sids:
                cont_norm = normalize_text(content)
                cont_words = set(cont_norm.split())
                if ans_norm and ans_norm in cont_norm:
                    supporting_evidence.append(content)
                elif a_words and a_words.issubset(cont_words):
                    supporting_evidence.append(content)
                elif len(target_words & cont_words) >= 2:
                    candidate_evidence.append(content)
        if turns:
            sessions.append((sid, turns))

    if not supporting_evidence and candidate_evidence:
        supporting_evidence.extend(candidate_evidence)

    return sessions, supporting_evidence


# ============================================================================
# 2. Per-item evaluation
# ============================================================================

@dataclass
class ItemResult:
    question_id: str
    question_type: str
    question: str
    expected_answer: str
    generated_answer: str
    exact_match: float
    substring_accuracy: float
    token_f1: float
    recall_at_k: float
    precision_at_k: float
    mrr: float
    stale_error: float
    retrieved_count: int
    stored_count: int
    tokens_used: int
    ingestion_time_s: float
    retrieval_time_s: float
    error: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)


def _fake_answer(query: str, retrieved: List[Any], expected: str) -> str:
    """
    Lightweight deterministic answer generator using retrieved memory content.
    Simulates a perfect LLM: returns the expected answer if it appears in any
    retrieved snippet, otherwise returns a fallback string.
    This enables end-to-end accuracy measurement without a live LLM.
    """
    exp_str = str(expected)
    exp_norm = normalize_text(exp_str)
    exp_words = set(exp_norm.split()) - _STOPWORDS

    for item in retrieved:
        mem = item[0] if isinstance(item, tuple) else item
        content = getattr(mem, "content", "") or ""
        cont_norm = normalize_text(content)
        cont_words = set(cont_norm.split())

        if exp_norm and exp_norm in cont_norm:
            return exp_str
        if exp_words and exp_words.issubset(cont_words):
            return exp_str

    return "I don't know."


def evaluate_item(
    item: Dict,
    strategy: TeamFlatMemoryStrategy,
    top_k: int = 5,
) -> ItemResult:
    """Run one benchmark item through the pipeline and return metrics."""
    question = item["question"]
    expected = item["answer"]
    q_type = item.get("question_type", "unknown")
    q_id = item.get("question_id", "?")

    strategy.reset()
    sessions, supporting_evidence = _item_to_sessions(item)

    # -- Ingestion --
    t0 = time.perf_counter()
    try:
        for sid, turns in sessions:
            strategy.process_session(
                turns, session_id=sid, current_timestamp=turns[-1]["timestamp"]
            )
    except Exception as exc:
        return ItemResult(
            question_id=q_id, question_type=q_type, question=question,
            expected_answer=expected, generated_answer="", exact_match=0.0,
            substring_accuracy=0.0, token_f1=0.0, recall_at_k=0.0,
            precision_at_k=0.0, mrr=0.0, stale_error=0.0,
            retrieved_count=0, stored_count=0, tokens_used=0,
            ingestion_time_s=0.0, retrieval_time_s=0.0,
            error=str(exc),
        )
    ingestion_time = time.perf_counter() - t0

    stored_count = len(strategy.memory_store)
    tokens_used = strategy.get_storage_usage()

    # -- Retrieval --
    t1 = time.perf_counter()
    try:
        retrieved = strategy.retrieve(question, top_k=top_k)
    except Exception as exc:
        return ItemResult(
            question_id=q_id, question_type=q_type, question=question,
            expected_answer=expected, generated_answer="", exact_match=0.0,
            substring_accuracy=0.0, token_f1=0.0, recall_at_k=0.0,
            precision_at_k=0.0, mrr=0.0, stale_error=0.0,
            retrieved_count=0, stored_count=stored_count, tokens_used=tokens_used,
            ingestion_time_s=ingestion_time, retrieval_time_s=0.0,
            error=str(exc),
        )
    retrieval_time = time.perf_counter() - t1

    retrieved_mems = [r[0] if isinstance(r, tuple) else r for r in retrieved]

    # -- Simulated answer --
    generated = _fake_answer(question, retrieved, expected)

    # -- Metrics --
    ans_m = evaluate_answer_accuracy(generated, expected, [expected])
    ret_m = evaluate_retrieval_quality(retrieved_mems, supporting_evidence)

    # Stale-memory error detection
    stale = 0.0
    pred_norm = normalize_text(generated)
    for mem in retrieved_mems:
        for pred_entry in mem.metadata.get("historical_predecessors", []):
            prev_val = normalize_text(pred_entry.get("previous_value", ""))
            if prev_val and prev_val in pred_norm:
                curr_val = normalize_text(mem.metadata.get("slot_value", ""))
                if curr_val and curr_val not in pred_norm:
                    stale = 1.0

    return ItemResult(
        question_id=q_id,
        question_type=q_type,
        question=question,
        expected_answer=expected,
        generated_answer=generated,
        exact_match=ans_m["exact_match"],
        substring_accuracy=ans_m["substring_accuracy"],
        token_f1=ans_m["token_f1"],
        recall_at_k=ret_m["recall_at_k"],
        precision_at_k=ret_m["precision_at_k"],
        mrr=ret_m["mrr"],
        stale_error=stale,
        retrieved_count=len(retrieved),
        stored_count=stored_count,
        tokens_used=tokens_used,
        ingestion_time_s=round(ingestion_time, 3),
        retrieval_time_s=round(retrieval_time, 4),
    )


# ============================================================================
# 3. Aggregate metrics helper
# ============================================================================

def _aggregate(results: List[ItemResult]) -> Dict[str, Any]:
    n = len(results)
    if n == 0:
        return {}

    def mean(key):
        vals = [getattr(r, key) for r in results if r.error is None]
        return round(sum(vals) / max(1, len(vals)), 4) if vals else 0.0

    by_cat: Dict[str, List[ItemResult]] = defaultdict(list)
    for r in results:
        by_cat[r.question_type].append(r)

    cat_metrics = {}
    for cat, cat_results in by_cat.items():
        cat_metrics[cat] = {
            "n": len(cat_results),
            "accuracy": round(sum(r.substring_accuracy for r in cat_results) / len(cat_results), 4),
            "recall_at_k": round(sum(r.recall_at_k for r in cat_results) / len(cat_results), 4),
            "token_f1": round(sum(r.token_f1 for r in cat_results) / len(cat_results), 4),
        }

    errors = [r for r in results if r.error]
    return {
        "n": n,
        "errors": len(errors),
        "accuracy_exact_match": mean("exact_match"),
        "accuracy_substring": mean("substring_accuracy"),
        "token_f1": mean("token_f1"),
        "recall_at_k": mean("recall_at_k"),
        "precision_at_k": mean("precision_at_k"),
        "mrr": mean("mrr"),
        "stale_error_rate": mean("stale_error"),
        "avg_stored_count": mean("stored_count"),
        "avg_tokens_used": mean("tokens_used"),
        "avg_ingestion_time_s": mean("ingestion_time_s"),
        "avg_retrieval_time_s": mean("retrieval_time_s"),
        "per_category": cat_metrics,
    }


# ============================================================================
# 4. Parameter grid (calibration on train split)
# ============================================================================

PARAM_GRID = [
    # Base defaults (matches existing repo defaults)
    dict(label="base_defaults",
         weights=ScoringWeights(w_r=0.25, w_i=0.25, w_f=0.20, w_t=0.15, w_c=0.15),
         min_imp=0.20, min_conf=0.30, budget=500),

    # Low recency, high importance + stability
    dict(label="low_recency_high_importance",
         weights=ScoringWeights(w_r=0.05, w_i=0.40, w_f=0.15, w_t=0.25, w_c=0.15),
         min_imp=0.30, min_conf=0.30, budget=500),

    # Tight admission gate (less noise)
    dict(label="tight_gate",
         weights=ScoringWeights(w_r=0.05, w_i=0.40, w_f=0.15, w_t=0.25, w_c=0.15),
         min_imp=0.50, min_conf=0.50, budget=500),

    # Larger budget with calibrated weights
    dict(label="large_budget",
         weights=ScoringWeights(w_r=0.05, w_i=0.40, w_f=0.15, w_t=0.25, w_c=0.15),
         min_imp=0.30, min_conf=0.30, budget=1000),

    # Balanced recency + importance
    dict(label="balanced",
         weights=ScoringWeights(w_r=0.15, w_i=0.35, w_f=0.15, w_t=0.20, w_c=0.15),
         min_imp=0.30, min_conf=0.30, budget=500),

    # Stability-dominant weights
    dict(label="stability_dominant",
         weights=ScoringWeights(w_r=0.05, w_i=0.30, w_f=0.15, w_t=0.35, w_c=0.15),
         min_imp=0.30, min_conf=0.30, budget=500),
]


def _make_strategy(params: Dict) -> TeamFlatMemoryStrategy:
    gate = CandidateAdmissionGate(
        min_importance_threshold=params["min_imp"],
        min_confidence_threshold=params["min_conf"],
    )
    return TeamFlatMemoryStrategy(
        total_budget=params["budget"],
        scoring_weights=params["weights"],
        admission_gate=gate,
    )


# ============================================================================
# 5. Training phase - calibrate on 470 chats
# ============================================================================

def run_calibration(
    train_items: List[Dict],
    top_k: int = 5,
    limit: Optional[int] = None,
    verbose: bool = True,
) -> Tuple[Dict, List[Dict]]:
    """
    Grid-search over PARAM_GRID on the train split.
    Returns (best_params, all_grid_results_summary).
    """
    if limit:
        train_items = train_items[:limit]

    sep = "=" * 70
    print(f"\n{sep}")
    print(f"CALIBRATION PHASE  |  {len(train_items)} train chats  |  {len(PARAM_GRID)} param sets")
    print(f"{sep}")

    grid_summaries: List[Dict] = []
    best_recall = -1.0
    best_params: Dict = PARAM_GRID[0]

    for pidx, params in enumerate(PARAM_GRID):
        label = params["label"]
        strategy = _make_strategy(params)
        results: List[ItemResult] = []

        t_start = time.time()
        for i, item in enumerate(train_items):
            result = evaluate_item(item, strategy, top_k=top_k)
            results.append(result)
            if verbose and (i + 1) % 50 == 0:
                elapsed = time.time() - t_start
                done_frac = (i + 1) / len(train_items)
                eta = elapsed / done_frac * (1 - done_frac)
                print(
                    f"  [{label}] {i+1}/{len(train_items)} "
                    f"| elapsed {elapsed:.0f}s | ETA {eta:.0f}s"
                )

        agg = _aggregate(results)
        recall = agg.get("recall_at_k", 0.0)
        acc = agg.get("accuracy_substring", 0.0)
        tok_f1 = agg.get("token_f1", 0.0)

        summary = {"params": label, **agg}
        grid_summaries.append(summary)

        print(
            f"  [{pidx+1}/{len(PARAM_GRID)}] {label:<35}"
            f" Recall@{top_k}={recall:.4f}  Acc={acc:.4f}  F1={tok_f1:.4f}"
        )

        if recall > best_recall:
            best_recall = recall
            best_params = params

    print(f"\n  Best params: [{best_params['label']}]  Recall@{top_k}={best_recall:.4f}")
    return best_params, grid_summaries


# ============================================================================
# 6. Test phase - evaluate on held-out 30 chats
# ============================================================================

def run_test(
    test_items: List[Dict],
    best_params: Dict,
    top_k: int = 5,
    verbose: bool = True,
) -> Tuple[Dict, List[ItemResult]]:
    """Evaluate best_params on the test split."""
    sep = "=" * 70
    print(f"\n{sep}")
    print(f"TEST PHASE  |  {len(test_items)} test chats  |  params: {best_params['label']}")
    print(f"{sep}")

    strategy = _make_strategy(best_params)
    results: List[ItemResult] = []

    t_start = time.time()
    for i, item in enumerate(test_items):
        result = evaluate_item(item, strategy, top_k=top_k)
        results.append(result)
        if verbose:
            ok = "OK" if result.recall_at_k > 0 else "--"
            print(
                f"  [{i+1:2d}/{len(test_items)}] {ok}  {result.question_id}  "
                f"recall={result.recall_at_k:.2f}  acc={result.substring_accuracy:.2f}  "
                f"F1={result.token_f1:.2f}  t={result.ingestion_time_s:.1f}s"
                + (f"  ERR:{result.error[:40]}" if result.error else "")
            )

    elapsed = time.time() - t_start
    agg = _aggregate(results)
    print(f"\n  Test completed in {elapsed:.1f}s")
    return agg, results


# ============================================================================
# 7. Report printer
# ============================================================================

def print_report(
    train_agg_grid: List[Dict],
    test_agg: Dict,
    best_params: Dict,
    top_k: int,
):
    sep = "=" * 70

    if train_agg_grid:
        print(f"\n{sep}")
        print("  CALIBRATION GRID SUMMARY  (train split - 470 chats)")
        print(sep)
        header = f"  {'Parameter Set':<35} {'Recall@K':>10} {'Acc':>8} {'F1':>8} {'MRR':>8}"
        print(header)
        print("  " + "-" * 65)
        for s in train_agg_grid:
            print(
                f"  {s['params']:<35} {s.get('recall_at_k', 0):>10.4f} "
                f"{s.get('accuracy_substring', 0):>8.4f} {s.get('token_f1', 0):>8.4f} "
                f"{s.get('mrr', 0):>8.4f}"
            )

    print(f"\n{sep}")
    print(f"  TEST RESULTS  (held-out 30 chats)  |  best params: {best_params['label']}")
    print(sep)
    metrics = [
        ("Accuracy (Exact Match)",      "accuracy_exact_match"),
        ("Accuracy (Substring)",        "accuracy_substring"),
        ("Token F1",                    "token_f1"),
        ("Precision@K",                 "precision_at_k"),
        ("Recall@K",                    "recall_at_k"),
        ("MRR",                         "mrr"),
        ("Stale Memory Error Rate",     "stale_error_rate"),
        ("Avg Stored Memories",         "avg_stored_count"),
        ("Avg Tokens Used",             "avg_tokens_used"),
        ("Avg Ingestion Time (s)",      "avg_ingestion_time_s"),
        ("Avg Retrieval Time (s)",      "avg_retrieval_time_s"),
    ]
    for label, key in metrics:
        val = test_agg.get(key, 0.0)
        print(f"  {label:<35} {val:>10.4f}")

    print(f"\n  Per-Category Breakdown (test split):")
    print(f"  {'Category':<35} {'N':>5} {'Accuracy':>10} {'Recall@K':>10} {'Token F1':>10}")
    print(f"  {'-'*35} {'-'*5} {'-'*10} {'-'*10} {'-'*10}")
    for cat, cm in sorted(test_agg.get("per_category", {}).items()):
        print(
            f"  {cat:<35} {cm['n']:>5} {cm['accuracy']:>10.4f} "
            f"{cm['recall_at_k']:>10.4f} {cm['token_f1']:>10.4f}"
        )
    print(f"\n  Errors: {test_agg.get('errors', 0)} / {test_agg.get('n', 0)}")
    print(sep)


# ============================================================================
# 8. CLI entry-point
# ============================================================================

def parse_args():
    p = argparse.ArgumentParser(
        description="Train/Test Evaluation Pipeline for Adaptive Long-Term Memory"
    )
    p.add_argument("--dataset", default="data/longmemeval/longmemeval_s.json",
                   help="Path to longmemeval_s.json")
    p.add_argument("--budget", type=int, default=500,
                   help="Token budget (used as default; grid also explores 1000)")
    p.add_argument("--top-k", type=int, default=5,
                   help="Memories retrieved per query (default: 5)")
    p.add_argument("--train-n", type=int, default=470,
                   help="Chats for calibration (default: 470)")
    p.add_argument("--test-n", type=int, default=30,
                   help="Held-out test chats (default: 30)")
    p.add_argument("--limit-train", type=int, default=None,
                   help="Cap train chats for fast debug (default: no cap)")
    p.add_argument("--skip-train", action="store_true",
                   help="Skip calibration and use low_recency_high_importance params")
    p.add_argument("--output-dir", default="outputs/train_test",
                   help="Output directory for JSON results")
    return p.parse_args()


def main():
    args = parse_args()
    os.makedirs(args.output_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

    # Load dataset
    train_items, test_items = _load_longmemeval(
        args.dataset, train_n=args.train_n, test_n=args.test_n
    )

    # Calibration phase
    if args.skip_train:
        best_params = next(
            p for p in PARAM_GRID if p["label"] == "low_recency_high_importance"
        )
        grid_summaries = []
        print(f"[skip-train] Using pre-selected params: {best_params['label']}")
    else:
        best_params, grid_summaries = run_calibration(
            train_items, top_k=args.top_k, limit=args.limit_train
        )
        grid_path = os.path.join(args.output_dir, f"calibration_grid_{timestamp}.json")
        with open(grid_path, "w", encoding="utf-8") as f:
            json.dump(grid_summaries, f, indent=2, default=str)
        print(f"  [saved] Calibration grid -> {grid_path}")

    # Test phase
    test_agg, test_results = run_test(
        test_items, best_params=best_params, top_k=args.top_k
    )

    # Save per-item results
    results_path = os.path.join(args.output_dir, f"test_results_{timestamp}.json")
    with open(results_path, "w", encoding="utf-8") as f:
        json.dump([r.to_dict() for r in test_results], f, indent=2, default=str)
    print(f"  [saved] Test results      -> {results_path}")

    # Save aggregate metrics
    agg_path = os.path.join(args.output_dir, f"metrics_{timestamp}.json")
    with open(agg_path, "w", encoding="utf-8") as f:
        meta = {
            "timestamp": timestamp,
            "best_params": best_params["label"],
            "train_n": args.train_n,
            "test_n": args.test_n,
            "top_k": args.top_k,
            "metrics": test_agg,
        }
        json.dump(meta, f, indent=2, default=str)
    print(f"  [saved] Aggregate metrics -> {agg_path}")

    # Print human-readable report
    print_report(grid_summaries, test_agg, best_params, args.top_k)


if __name__ == "__main__":
    main()
