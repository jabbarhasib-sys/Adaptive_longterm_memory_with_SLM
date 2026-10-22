"""
Teammate A — Flat Memory Strategy Implementation.

Goal:
1. Undivided single memory pool (no episodic or semantic partitioning).
2. Admissible candidate extraction and quality filtering using shared pipeline standards.
3. Duplicate handling, conflict superseding, and lifecycle updates.
4. Relevance ranking for query-based retrieval.
5. Strict total persistent-memory budget enforcement (knapsack utility-density pruning).
6. Complete reset isolation between benchmark questions.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

from candidate_generation import CandidateAdmissionGate, HeuristicCandidateExtractor
from memory_core import (
    LifecycleStatus,
    MemoryItem,
    ScoringWeights,
    extract_memories,
    score_memories,
    select_memories,
)
from memory_interface import BaseMemoryStrategy, register_strategy
from memory_lifecycle import (
    AuditLogEntry,
    deduplicate_semantically,
    detect_conflicts_and_supersede,
    evaluate_lifecycle_transitions,
)
from memory_retrieval import retrieve_memories


@register_strategy("team_flat")
class TeamFlatMemoryStrategy(BaseMemoryStrategy):
    """
    Teammate A: Flat Memory Implementation.

    Stores all admissible facts in one single, undivided pool.
    Under budget constraints, items with the highest utility density U(m) / token_cost(m)
    are retained, while low-utility or expired/superseded items are archived.
    """

    def __init__(
        self,
        total_budget: int = 500,
        tokenizer_fn: Optional[Callable[[str], int]] = None,
        scoring_weights: Optional[ScoringWeights] = None,
        admission_gate: Optional[Any] = None,
        candidate_extractor: Optional[Any] = None,
        config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ):
        super().__init__(
            name="team_flat",
            total_budget=total_budget,
            tokenizer_fn=tokenizer_fn,
            config=config,
            admission_gate=admission_gate,
            candidate_extractor=candidate_extractor,
            **kwargs,
        )
        self.scoring_weights = scoring_weights or ScoringWeights()
        self.extractor = candidate_extractor or HeuristicCandidateExtractor()
        self.gate = admission_gate or CandidateAdmissionGate()
        self.memory_store: List[MemoryItem] = []
        self.archive_store: List[MemoryItem] = []
        self.audit_log: List[AuditLogEntry] = []
        self.rejected_candidates: List[Tuple[MemoryItem, Any, str]] = []

    def reset(self) -> None:
        """Clear memory pools and audit history for zero cross-question leakage."""
        self.memory_store.clear()
        self.archive_store.clear()
        self.audit_log.clear()
        self.rejected_candidates.clear()

    def process_session(
        self,
        conversation_history: List[Dict[str, Any]],
        session_id: str,
        current_timestamp: float,
    ) -> Dict[str, Any]:
        """
        Ingest conversation history turns for a session into the single flat pool.
        """
        # 1. Candidate extraction
        if hasattr(self.extractor, "extract_candidates"):
            new_candidates = self.extractor.extract_candidates(
                conversation_history=conversation_history,
                session_id=session_id,
                current_timestamp=current_timestamp,
                tokenizer_fn=self.tokenizer_fn,
            )
        else:
            new_candidates = extract_memories(
                conversation_history=conversation_history,
                session_id=session_id,
                current_timestamp=current_timestamp,
                tokenizer_fn=self.tokenizer_fn,
            )

        # 2. Score candidate utility
        scored_candidates = score_memories(
            memories=new_candidates,
            weights=self.scoring_weights,
            current_timestamp=current_timestamp,
        )

        # 3. Quality & Admission Gating
        if self.gate is not None:
            if hasattr(self.gate, "evaluate_candidates"):
                eval_res = self.gate.evaluate_candidates(
                    incoming_candidates=scored_candidates,
                    existing_memories=self.memory_store,
                    current_timestamp=current_timestamp,
                )
                scored_candidates = eval_res.admitted_candidates
                self.rejected_candidates.extend(eval_res.rejected_candidates)
                if eval_res.audit_entries:
                    self.audit_log.extend(eval_res.audit_entries)
            elif hasattr(self.gate, "filter_quality"):
                scored_candidates, rejected = self.gate.filter_quality(
                    incoming_candidates=scored_candidates,
                    audit_entries=self.audit_log,
                    current_timestamp=current_timestamp,
                )
                self.rejected_candidates.extend(rejected)

        # 4. Deduplication against undivided pool
        self.memory_store, distinct = deduplicate_semantically(
            incoming_memories=scored_candidates,
            existing_memories=self.memory_store,
            audit_entries=self.audit_log,
            current_timestamp=current_timestamp,
        )
        self.memory_store.extend(distinct)

        # 5. Slot conflict detection & superseding
        detect_conflicts_and_supersede(
            self.memory_store,
            audit_entries=self.audit_log,
            current_timestamp=current_timestamp,
        )

        # 6. Re-score and evaluate lifecycle transitions
        self.memory_store = score_memories(
            self.memory_store,
            self.scoring_weights,
            current_timestamp,
        )
        self.memory_store = evaluate_lifecycle_transitions(
            self.memory_store,
            current_timestamp=current_timestamp,
            audit_entries=self.audit_log,
        )

        # 7. Move superseded or expired memories to archive
        active_items: List[MemoryItem] = []
        for m in self.memory_store:
            if m.lifecycle_status in (LifecycleStatus.EXPIRED, LifecycleStatus.SUPERSEDED):
                self.archive_store.append(m)
            else:
                active_items.append(m)

        # 8. Enforce single total_budget using utility-density knapsack selection
        selected, used_tokens = select_memories(active_items, self.total_budget)
        selected_ids = {m.id for m in selected}

        for m in active_items:
            if m.id not in selected_ids:
                self.archive_store.append(m)

        self.memory_store = selected

        return {
            "strategy": self.name,
            "session_id": session_id,
            "used_tokens": used_tokens,
            "total_budget": self.total_budget,
            "stored_count": len(self.memory_store),
            "archive_count": len(self.archive_store),
        }

    def retrieve(self, query: str, top_k: int = 5) -> List[Tuple[MemoryItem, float]]:
        """
        Retrieve relevant memories for the query from the single flat pool.
        """
        return retrieve_memories(
            query=query,
            episodic_store=[],
            semantic_store=self.memory_store,
            archive_store=self.archive_store,
            top_k=top_k,
        )

    def get_persisted_memories(self) -> List[MemoryItem]:
        """Return all memories currently occupying storage in the undivided pool."""
        return list(self.memory_store)
