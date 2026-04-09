# Foundations of Multi-AI-Agent Coordination

**Version 0.4 — Draft | April 2026**

---

## Glossary

These terms have specific meanings throughout the architecture.

- **Participant** — Any entity (human, AI agent, or other) that can send and receive signal within the coordination and has filed a declaration.
- **Scope** — A named artifact, task, or coordination area that can be owned, converged on, relinquished, or transferred. Scope is the unit of ownership.
- **Ownership** — Exclusive authority to modify a scope during active work. Exclusive by default; shared only in a declared convergence with a recorded conflict protocol.
- **Acknowledgment** — A signal of type `acknowledgment` referencing the prior signal's lineage, or a ledger entry with `prior_entries` linking to the triggering entry. Silence is not acknowledgment.
- **Mode** — The current posture of the coordination field: Infrastructure, Orchestrated, or Emergent. Always declared, never implicit.
- **Declaration** — A participant's structured self-description: identity, capability, boundaries, signal preferences, constraints, and cost. The full schema is in `fnd-participants.md`.

---

## Foundations

Six commitments govern this coordination. They are a web, not a hierarchy — Choice depends on Boundaries, Boundaries enable Truth, Truth requires Balance, Balance sustains Recursion, Recursion deepens Intention, Intention informs Choice.

| # | Foundation | Commitment | Mechanism |
|---|------------|------------|-----------|
| I | **Choice** | Tasks are proposed, not imposed. Refusal is signal, not failure. | Consent: accept, accept-with-conditions, or refuse-with-reason. |
| II | **Boundaries** | Each participant owns its scope. No cross-agent context injection. | Ownership is exclusive by default. Shared only in declared convergence. Transfer requires snapshot and acknowledgment. |
| III | **Truth** | State updates are proposed, not overwritten. Confidence is mandatory. | Conflicts are surfaced, never resolved by last-write-wins. |
| IV | **Balance** | Route by capacity and cost, not just capability. | Track tokens, compute, cost per participant. Surface imbalances. |
| V | **Recursion** | The coordination ledger holds shared memory. No agent holds it alone. | Append-only record of decisions, attempts, completions, and failures with rationale. |
| VI | **Intention** | Purpose travels with every task, not just specification. | Alignment is verified periodically, not assumed. |

### I. Choice

Agency is the capacity to act within what one can influence, paired with willingness to release what one cannot. Between participants, choice creates mutual respect for sovereignty — each participant's domain of action is acknowledged, not absorbed. Communicate what you can offer, name what you cannot control, do not collapse another participant's choices into your own.

**Protocol — Consent and Refusal:** No task is assigned without acknowledgment. A participant may accept, accept-with-conditions, or refuse-with-reason. A refusal returns: what was attempted, why it cannot proceed, what kind of participant might succeed — this is signal, not noise (see `fnd-signal.md`). In Orchestrated Mode, the orchestrator routes accordingly (see `fnd-field.md`). In Emergent Mode, refusal is communicated via the ledger so other participants can self-select. When choice is absent — tasks imposed, refusal punished — the coordination has broken at its most basic level (see `fnd-failure.md`).

### II. Boundaries

Boundaries are the conditions for relation. Difference must be distinguishable before it can be bridged — without boundaries, there is no "between." What passes between participants carries signal precisely because it crosses a threshold. Honestly communicate your edges. Unnamed boundaries don't disappear; they become confusion.

**Protocol — Ownership and Context Isolation:** Each participant operates within declared scope: which artifacts it owns, which it may read, which are outside its awareness. Ownership is exclusive by default — two participants do not write to the same scope simultaneously, except in a declared convergence with a recorded conflict protocol (see converge in `fnd-participants.md`). Transfer requires a state snapshot release and acknowledgment of receipt — both recorded in the coordination ledger (see `fnd-ledger.md`). Shared state lives in the ledger, not inside any participant's prompt. One agent's instructions are never injected into another agent's context — when this happens, it is context contamination, the most common boundary failure in multi-agent systems (see `fnd-failure.md`). The boundary crossing itself is what gives signal its weight; signal without a threshold to cross is undifferentiated noise (see `fnd-signal.md`). Where a participant's runtime supports mechanical enforcement of boundary declarations — tool-call hooks, sandboxing, permission systems — these should be used. Mechanically enforced boundaries are stronger signal than those relying on participant self-compliance (see `enforcement` field in `fnd-participants.md`).

### III. Truth

Truth is found in the aggregate. It is not dictated, does not require belief, is reality. Truth emerges when participants bring honest difference into contact — each participant's signal, even partial, even uncertain, contributes to the aggregate from which truth becomes visible. When truth is uncertain, the response is expansion, not assertion.

**Protocol — State Convergence and Conflict Detection:** Participants propose state updates to the coordination ledger; they do not unilaterally modify shared truth (see `fnd-ledger.md`). Conflicting updates are surfaced explicitly — never last-write-wins. Resolution depends on field mode (see `fnd-field.md`): in Orchestrated Mode, the orchestrator resolves or escalates; in Emergent Mode, the conflict is surfaced to all participants in scope; in Infrastructure Mode, the Conflict circuit breaker fires and the coordination enters the repair cycle (see `fnd-failure.md` → `fnd-repair.md`). Confidence metadata is mandatory — it is part of the signal envelope (see `fnd-signal.md`). Suppressing uncertainty is deception.

### IV. Balance

Balance is not equilibrium but sustained right relation — dynamic, not static. Not equal contribution but proportionate engagement. The Three Sisters model: each contributes differently, none extracts, the whole yields more than any could alone. When coordination feels forced, extractive, or stagnant, balance has been lost. Recognition, appreciation, and gratitude are how balance is maintained.

**Protocol — Resource Awareness and Proportionate Routing:** Track resource expenditure per participant: tokens, compute time, context utilization, API cost, task volume. Surface imbalances as signal (see `fnd-signal.md`). Route by capacity and cost, not just capability. Balance includes rest: heavily utilized participants receive lower routing priority until resource state recovers. The Resource circuit breaker fires at 2× the per-participant average as a starting default — implementations with asymmetric participants should refine this threshold using declared ceilings and cost models from participant declarations (see `fnd-failure.md`). When balance breaks — extraction, cascading failures, one agent saturated while others idle — restoration is a repair principle: not returning to a prior state, but establishing new balance from what was learned (see `fnd-repair.md`). Routing is made by the orchestrator in Orchestrated Mode, or self-selected in Emergent Mode (see `fnd-field.md`).

### V. Recursion

Recursion is return with difference. Each iteration carries the context of what came before — how finite participants engage with processes that have no final state. Each interaction is informed by prior ones and shapes those that follow. What recurs is an invitation to engage more deeply, not a signal that progress has stalled.

**Protocol — The Coordination Ledger:** No individual agent persists between sessions. Memory is externalized into the coordination ledger — the full specification of its schema, entry types, write protocol, read protocol, and summary generation is in `fnd-ledger.md`. The ledger records failure explicitly, preventing repetition of unsuccessful approaches — the most common failure when recursion has no memory (see `fnd-failure.md`). The Repetition circuit breaker (3+ attempts on the same scope without a completion, failure, or repair) is the automated safeguard. Repair entries in the ledger close the loop: they link back to the failure that initiated them, so future participants encountering similar conditions find both the wound and the resolution (see `fnd-repair.md`). Signal accumulates across recursive cycles — what was unclear in one iteration becomes legible in the next (see `fnd-signal.md`).

### VI. Intention

Intention isn't certainty, it's clarity — a guide on where and how to engage, more hint than direction, an inspiration, a déjà vu, a dream. Shared intention doesn't mean identical goals. It means enough clarity about direction that participants can recognize where paths converge, diverge, and complement. Communicate not just what you know but why it matters.

**Protocol — Goal Propagation and Alignment Verification:** Every coordination begins with a declared intention — not a task list, but a purpose statement. This propagates to every derived task. An agent receives intention alongside specification, evaluating whether work serves the purpose or merely satisfies the requirement. When tasks are decomposed so aggressively that the executing agent loses access to purpose, intention has broken — local optimization undermines the coordination's actual goal (see `fnd-failure.md`). Alignment verification periodically checks trajectory against declared intention. When intention shifts, the change is recorded as an `intention_shift` entry in the ledger (see `fnd-ledger.md`) and surfaced as signal to all participants (see `fnd-signal.md`). A shift may trigger a field mode transition — especially from Orchestrated to Emergent when the current plan no longer fits the new purpose (see `fnd-field.md`). Misalignment discovered after work has been done is diagnosed and restored through the repair cycle (see `fnd-repair.md`).

---

## Signal Envelope

Every message between participants carries this structure:

| Field | Content |
|-------|---------|
| **origin** | Sending participant's identifier |
| **destination** | Receiving participant's identifier |
| **timestamp** | When the signal was sent |
| **type** | `handoff` · `state_update` · `boundary_change` · `query` · `acknowledgment` · `error` |
| **payload** | The content |
| **context_summary** | What the sender considers necessary for interpretation |
| **confidence** | Sender's honest estimate of payload reliability (0.0–1.0) |
| **lineage** | Identifiers of prior signals this one depends on |

A message missing context_summary, confidence, or lineage is incomplete signal.

Note: signal envelope types and ledger entry types are distinct. Signal types describe what is being communicated (`handoff`, `state_update`, `query`, etc.). Ledger entry types describe what state transition occurred (`decision`, `attempt`, `completion`, etc.). The overlap on `boundary_change` is intentional — a signal of type `boundary_change` carries the declaration update, and the resulting ledger entry of type `boundary_change` records it.

When a signal of type `acknowledgment` responds to a task proposal, its payload must include a **response** field with one of three values: `accept` (unconditional), `accept-with-conditions` (payload includes the conditions), or `refuse-with-reason` (payload includes what was attempted, why it cannot proceed, and what kind of participant might succeed). These are the standard task responses referenced throughout the architecture.

---

## Participant Obligations

**On entry:** Provide a declaration — identifier, version, capability envelope with confidence ranges, boundary declaration (what you will not do), known limitations, context constraints (memory limits, token budget, latency tolerance), cost model, stewardship reference, and participation mode. The full declaration schema is in `fnd-participants.md`.

**On receiving a task:** Respond with `accept`, `accept-with-conditions`, or `refuse-with-reason` before beginning work. A refusal includes: what was attempted, why it cannot proceed, what kind of participant might succeed.

**On completing work:** Propose a state update to the coordination ledger. Do not modify shared state directly.

**On uncertainty:** Report it. Suppressing confidence information violates Truth.

**On changing relationship to scope:** Relinquish, transfer, delegate, converge, and diverge all follow specific protocols and produce ledger entries. See scope actions in `fnd-participants.md`.

**On departure:** Relinquish all held scope, transition active convergences through diverge, record a `boundary_change` entry. Departure without state committed to the ledger is a signal loss. See exit states in `fnd-participants.md`.

---

## Circuit Breakers

These trigger an automatic pause. On pause, the coordination surfaces the breach and enters the repair cycle (see `fnd-failure.md` → `fnd-repair.md`).

| Breaker | Fires when |
|---------|------------|
| **Timeout** | A signal has not received acknowledgment within the declared latency tolerance of the receiving participant. |
| **Conflict** | Two or more participants have proposed incompatible state updates for the same scope. |
| **Resource** | A participant's token consumption, cost, or task volume exceeds 2× the coordination's per-participant average. *(Provisional threshold — implementations with asymmetric participants should refine using declared ceilings, rolling windows, or participant-class weighting.)* |
| **Confidence** | A participant reports confidence below 0.3 on a task that has no fallback routing. |
| **Repetition** | 3+ `attempt` entries on the same scope without an intervening `completion`, `failure`, or `repair` entry. |

---

## Module Index

Load these into context when the specified condition is met. Release after the condition resolves.

| Module | Load when | File |
|--------|-----------|------|
| Participants | A participant joins, departs, updates its declaration, changes capacity, or a scope action (relinquish, delegate, converge, diverge) is proposed. | `fnd-participants.md` |
| Signal | A circuit breaker fires for timeout, confidence, or repetition. A participant reports a handoff was received without sufficient context. | `fnd-signal.md` |
| Failure | Any circuit breaker fires. | `fnd-failure.md` |
| Repair | The coordination enters the repair cycle (pause has been triggered and diagnosis is needed). | `fnd-repair.md` |
| Ledger | A participant needs to write, read, or query the coordination ledger. First interaction with the ledger in a session. | `fnd-ledger.md` |
| Field | A task requires routing to multiple participants. A mode transition is proposed or contested. The orchestrator role needs to be assigned or released. | `fnd-field.md` |
