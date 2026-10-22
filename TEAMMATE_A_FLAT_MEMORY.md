# Teammate A — Flat Memory Strategy (`team_flat`)

**Author:** Jabbar Hasib
**Branch:** `feature/flat-memory`
**File:** `strategies/flat_team.py`
**Strategy Name (registered):** `team_flat`

---

## What I Built

I implemented the **Flat Memory Strategy** for the shared Adaptive Long-Term Memory pipeline. My role as Teammate A was to build a memory system that stores facts in a **single, undivided pool** — no episodic/semantic partitioning — while still enforcing a strict persistent token budget, quality admission gating, conflict resolution, and relevance-based retrieval.

This acts as the **baseline comparison strategy** against Fixed Hierarchical (70/30, 50/50) and Adaptive Hierarchical strategies.

---

## Core Idea

> **One pool. Every admitted fact competes equally for space based on its utility-density.**

Unlike hierarchical strategies that reserve fixed or dynamic quotas per memory type, the flat approach lets the math decide what stays, using a 5-factor utility scoring formula and knapsack density pruning.

---

## Processing Pipeline (8 Stages)

When conversation turns arrive, the strategy runs them through these ordered stages:

```
RAW TURNS
   │
   ▼
[1] Candidate Extraction      ── breaks compound sentences → atomic facts
   │
   ▼
[2] 5-Factor Utility Scoring  ── scores every candidate U(m) ∈ [0.0, 1.0]
   │
   ▼
[3] Admission Gate            ── rejects fillers, low-confidence, noise
   │                                         └── REJECTED
   ▼
[4] Semantic Deduplication    ── merges near-duplicates, increments frequency
   │
   ▼
[5] Slot Conflict Detection   ── supersedes stale facts (e.g. old employer)
   │                                         └── SUPERSEDED → archive
   ▼
[6] Lifecycle Evaluation      ── ACTIVE / STABLE / EXPIRED transitions
   │
   ▼
[7] Knapsack Budget Pruning   ── keeps highest U(m)/token_cost items ≤ B tokens
   │                                         └── EVICTED → archive
   ▼
[8] Active Flat Pool          ── used for retrieval
```

---

## Mathematical Formulation

### Utility Score Formula

Every memory item `m` gets an aggregated utility score:

```
U(m) = w_r·R(m) + w_i·I(m) + w_f·F(m) + w_t·T(m) + w_c·C(m)
```

| Symbol | Name | Weight | How It's Calculated |
|:---|:---|:---:|:---|
| `R(m)` | **Recency** | `0.25` | Exponential decay: `e^(−λ·Δt)`. Facts mentioned recently score higher. |
| `I(m)` | **Importance** | `0.25` | Slot-based: permanent facts (employer, city) ≈ `0.85`; temp tasks ≈ `0.40`. |
| `F(m)` | **Frequency** | `0.20` | Normalized repeat mentions: `log(1+f) / log(1+5)`. Reinforced facts rank higher. |
| `T(m)` | **Temporal Stability** | `0.15` | Durable profile facts ≈ `0.90`; session-only tasks ≈ `0.20`. |
| `C(m)` | **Consistency Confidence** | `0.15` | Discounted by hedging words ("maybe", "I think") by 30–50%. |

> All weights sum to `1.0`. Scores are always in `[0.0, 1.0]`.

---

### Admission Gate Thresholds

Before any fact enters storage, it must pass 3 gates:

| Gate | Condition | Action on Fail |
|:---|:---|:---|
| Informativeness | Must not be a filler or pleasantry | Discarded |
| Confidence | `C(m) ≥ 0.30` | Rejected (low confidence) |
| Importance | `I(m) ≥ 0.20` | Rejected (not informative enough) |

---

### Semantic Deduplication Threshold

```
similarity = 0.5·Jaccard(tokens_a, tokens_b) + 0.5·GestaltRatio(a, b)

if similarity ≥ 0.80:
    → merge: increment frequency, update timestamp, preserve provenance
    → do NOT consume new storage budget
```

---

### Slot-Based Conflict Superseding

Slot patterns detected (examples):
- `employer` — "I work at X", "I joined Y"
- `residence_city` — "I live in X", "I moved to Y"
- `editor_preference` — "I use X for coding"

When a **newer** fact matches an **existing** slot with a **different value**:

```
older_fact.lifecycle_status → SUPERSEDED   (moved to archive, kept for history)
newer_fact.predecessor_ids  ← [older_fact.id]   (audit chain preserved)
```

This achieves **0.0% Stale Memory Error** — superseded facts never appear in active retrieval.

---

### Knapsack Budget Pruning

When total tokens exceed budget `B`:

```
Density(m) = U(m) / token_cost(m)
```

Items are ranked by density descending. A greedy 0/1 knapsack fills the budget with the highest-density items. Evicted items move to the archive (not deleted).

---

### Retrieval Relevance Scoring

For query `q` against memory `m`:

```
Relevance(q, m) = 0.5·LexicalOverlap(q, m) + 0.3·SlotMatch(q, m) + 0.2·U(m)
```

- **Current queries** → only `ACTIVE` / `STABLE` memories searched.
- **Historical queries** ("where did I work before?") → predecessor chain traced.

---

## Files I Created

| File | Purpose |
|:---|:---|
| `strategies/flat_team.py` | Core implementation — `TeamFlatMemoryStrategy` registered as `team_flat` |
| `test_team_flat.py` | 5 unit tests covering all key behaviours |

---

## Live Verification Commands

Run these in order in your terminal from the project root:

### Step 1 — Confirm Strategy is Auto-Discovered
```powershell
python run_experiments.py --list-strategies
```
Look for `team_flat` in the output list.

---

### Step 2 — Single Question Quick Test
```powershell
python run_experiments.py --strategy team_flat --dataset synthetic --budgets 500 --limit 1
```
**Expected result:**
```
| team_flat | 500 | 100.0% | 0.333 | 100.0% | 1.000 | 0.0% | 7.0 | ~0.5 |
```

---

### Step 3 — Full Synthetic Benchmark Across 3 Budgets
```powershell
python run_experiments.py --strategy team_flat --dataset synthetic --budgets 50 150 500
```
Shows accuracy, recall, stale error rate, and storage usage at tight (50), medium (150), and generous (500) token budgets.

---

### Step 4 — Run Dedicated Unit Tests
```powershell
python -m unittest test_team_flat.py -v
```
**Expected result:**
```
test_auto_discovery_and_registration ... ok
test_budget_enforcement ... ok
test_conflict_superseding ... ok
test_intake_and_retrieval ... ok
test_reset_isolation ... ok
Ran 5 tests in 0.008s — OK
```

---

### Step 5 — Verify Full Repository (100 Tests)
```powershell
python -m unittest discover -v -s . -p "test_*.py"
```
**Expected result:**
```
Ran 100 tests in ~0.18s — OK
```
All 100 tests passing means my changes integrate cleanly with the shared pipeline without breaking anything.

---

## Summary

| Property | Value |
|:---|:---|
| Strategy Name | `team_flat` |
| Memory Pool Type | Single undivided flat pool |
| Token Budget Enforced | ✅ Strict (knapsack density pruning) |
| Stale Memory Errors | ✅ 0.0% (slot-based superseding) |
| Admission Gating | ✅ Confidence, importance, filler filters |
| Deduplication | ✅ Jaccard + Gestalt similarity merge |
| Reset Isolation | ✅ Zero cross-question leakage |
| Test Coverage | ✅ 5 unit tests, all passing |
| Full Suite Impact | ✅ 100/100 tests pass |
