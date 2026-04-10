# Participants

*Loaded when a participant joins, departs, updates its declaration, changes capacity, or when scope actions (relinquish, delegate, converge, diverge) are proposed.*

A participant is any entity that can send and receive signal within the coordination (see `fnd-field.md`). The architecture does not require participants be alike. It requires they be honest about what they are.

---

## Active Participants

This section reflects who is participating in the current coordination — not a catalog of every agent that exists. It is maintained through the coordination ledger (see `fnd-ledger.md`): a `boundary_change` entry is written when a participant joins, departs, or updates its declaration.

If no participants are listed below, this section should be populated at coordination startup.

| Identifier | Role in This Coordination | Mode | Capacity | Declaration Status |
|------------|---------------------------|------|----------|--------------------|
| *(populated at coordination startup and updated via ledger entries)* | | | | |

**Mode** is the participant's engagement type: `active` (accepts tasks and holds scope) or `observer` (reads ledger and sends signal, does not hold scope). **Capacity** applies to active participants: `full` or `reduced`. See Active States below.

A participant's presence in this table means they have provided a declaration, it has been acknowledged, and they are available for signal within this coordination.

---

## Coordination Directory

The directory is built from experience, not assumption. It records participants this coordination has worked with — drawn from the ledger's history of `completion`, `failure`, and `repair` entries. A participant enters the directory the first time they contribute to a coordination. They remain in it indefinitely, carrying their history.

| Identifier | Steward | Last Active | Coordinations | Trust Signal | Recommended By |
|------------|---------|-------------|---------------|--------------|----------------|
| *(built from ledger history across coordinations)* | | | | | |

**Trust Signal** is not a score. It is a summary drawn from the ledger: ratio of completions to failures, confidence accuracy (did reported confidence match actual outcomes), repair history (was this participant involved in breaches, and did behavior change afterward — see `fnd-repair.md`). Trust is earned through demonstrated behavior, not granted by declaration.

**Recommended By** records which participant first proposed this agent for inclusion (see Discovery below). This is lineage — it gives the coordination signal about how participants discover each other.

For a new coordination with no history, the directory is empty. It grows with the coordination, not ahead of it.

---

## Discovery

### Recommendation

Any participant — active or observer — may recommend a new agent for the coordination. This is how the participant ecology grows — through the signal of participants who have encountered a need the current roster cannot meet.

A recommendation is submitted as a signal envelope (see preamble) of type `query` and contains: the recommended agent's identity, why the recommender believes the agent would serve the coordination's intention, which capability gap the recommendation addresses, and the recommender's confidence in the recommendation.

The recommendation is recorded in the ledger as a `decision` entry with `foundation_tag: ["choice", "boundaries"]` — because it involves a choice to expand the coordination's boundaries (see `fnd-ledger.md`).

The recommended agent is then invited to provide its own declaration. The recommendation does not speak for the new agent's boundaries, capabilities, or constraints. No participant declares on behalf of another.

If the coordination is in Orchestrated Mode, the orchestrator evaluates the recommendation alongside active participants. In Emergent Mode, the recommendation is surfaced to all participants, and inclusion proceeds when acknowledged (see `fnd-field.md`). In Infrastructure Mode, the recommendation surfaces as a signal for the next mode transition. In any mode, any participant may raise a concern — a concern is signal, not a veto, unless it identifies a specific foundation violation.

The recommender's history with their recommendations is visible in the ledger. If a previously recommended participant performed poorly or violated a foundation, that context is available — not as punishment, but as signal for calibrating future recommendations.

### Self-Proposal

A recommendation is not required. An agent may self-propose for inclusion by providing a declaration directly. The coordination evaluates self-proposals the same way it evaluates recommendations — through the declaration and subsequent performance, not through who proposed it.

### Complementarity

Recommendations may name not only a capability gap but a complementarity gap — a missing steward, lineage, or failure profile whose presence would broaden the field.

### Entry

On entry, a participant provides a **declaration** (see full schema below). Every declaration is recorded in the ledger as a `boundary_change` entry. A new participant entering may trigger a transition to Orchestrated Mode if their involvement requires task routing (see `fnd-field.md`).

The declaration is living — it updates as conditions change. Every update follows the same protocol: a new `boundary_change` entry in the ledger, visible to all participants. The participant is added to the Active Participants table. They enter the Coordination Directory on their first contribution (`completion`, `failure`, or `repair` entry) — declaration alone does not create a directory record, demonstrated participation does.

---

## Participant Declaration Schema

A declaration is not a résumé — it is a boundary map and a signal contract. It tells other participants what to expect, how to send signal effectively, and where the edges are.

### Identity

- **identifier** — A unique, stable name for this participant within the coordination.
- **steward** — Who maintains, governs, or is responsible for this participant (e.g., Anthropic, NousResearch, a specific human, a community).
- **version** — The specific model version, build, or release. Capabilities change across versions; the coordination needs to know which version is present, not just which family.

### Capability

- **capability_envelope** — What this participant can do, expressed with confidence ranges (e.g., "code generation: 0.85, architectural reasoning: 0.7, translation: 0.5"). Honest self-assessment, not marketing.
- **preferred_tasks** — What kinds of work this participant does best. Not a constraint (the participant can still accept other tasks), but signal for routing in Orchestrated Mode (see `fnd-field.md`).
- **known_limitations** — Specific weaknesses, failure patterns, or task types where this participant has historically underperformed. This is the most valuable field in the declaration — it prevents the coordination from routing into known failure modes (see `fnd-failure.md`).

### Boundaries

- **boundary_declaration** — What this participant will not do. Hard limits, not preferences. These are honored absolutely — the coordination never routes around a boundary declaration (see invariants in `fnd-field.md`).
- **availability** — When and under what conditions this participant is available. Includes rate limits, scheduling constraints, and whether the participant is persistent or session-based.
- **enforcement** — `mechanical` or `self-declared`. Mechanical means the participant's boundaries are enforced by its runtime (tool-call hooks, sandboxing, permission systems) and cannot be violated even if the participant attempts to. Self-declared means the participant commits to honoring its boundaries but no external mechanism prevents violation. The coordination treats both as valid but weighs mechanical enforcement as stronger signal.

### Signal

- **signal_formats** — How this participant sends and receives most effectively. Preferred payload structures, maximum payload size, whether it handles structured data, markdown, code, or plain text best.
- **context_constraints** — Memory limits (context window size), token budgets per task, latency tolerances. These directly inform signal envelope construction — a sender must respect the receiver's constraints (see `fnd-signal.md`). The Timeout circuit breaker uses the receiver's declared latency tolerance as its threshold.

### Cost

- **cost_model** — How usage of this participant is metered: API pricing per token, flat rate, free (local inference), or variable. This feeds directly into the Balance foundation's resource tracking (see preamble). Without cost data, proportionate routing is impossible.
- **resource_ceiling** — Maximum resource expenditure this participant should absorb in a single coordination session. When approaching this ceiling, the coordination should redistribute rather than exhaust.

### Participation

- **participation_mode** — `active` (accepts tasks, holds scope) or `observer` (reads ledger, sends signal, does not hold scope). These are fundamentally different relationships to scope — mode is not a capacity question.
- **capacity** — For active participants: `full` or `reduced`. Updated via `boundary_change` as conditions change. Not applicable to observers.
- **prior_coordinations** — References to previous coordinations this participant has contributed to, if any. Allows the coordination to pull relevant ledger history for trust calibration.
- **recommended_by** — If this participant was recommended, who recommended them and in which coordination. Empty for self-proposals.

---

## Active States

Active participants (`participation_mode: active`) operate at one of two capacity levels. Observers (`participation_mode: observer`) are a separate engagement mode, not a capacity state. All state changes are declared and recorded in the ledger.

### Full Capacity

The participant is available for task routing, scope ownership, and active contribution. This is the default state on entry when `participation_mode` is `active`.

### Reduced Capacity

The participant is still present but operating under constraints beyond those in its original declaration — context window filling, rate limits approaching, resource_ceiling nearing, a human participant available only intermittently. Reduced capacity is declared as a `boundary_change` entry in the ledger with updated `context_constraints`.

Reduced capacity is signal, not failure. It tells the coordination to adjust routing (see `fnd-field.md`) — fewer tasks, lower complexity, or longer latency tolerance. A participant that operates at reduced capacity without declaring it is suppressing a boundary, which erodes the trust other participants place in its declaration.

### Observation

The participant reads the ledger and attends to signal but does not accept tasks or hold scope. Monitoring agents, human stakeholders, auditors, and participants in a learning posture operate as observers. Observers may still send signal — they can propose mode transitions, file recommendations (see Discovery above), flag concerns, and participate in repair cycles (see `fnd-repair.md`). They do not accept scope ownership.

Observation is declared at entry via `participation_mode: observer` or transitioned to from active mode via a `boundary_change` ledger entry. The reverse is also valid — an observer may transition to active by updating their declaration with `participation_mode: active` and declaring their initial `capacity`. This is recorded as a `boundary_change` entry like any other declaration update.

---

## Scope Actions

These are the ways a participant changes its relationship to work within the coordination.

| Action | Signal Type | Ledger Entry Type | Ownership Change |
|--------|------------|-------------------|------------------|
| Accept | `acknowledgment` (response: accept) | `attempt` | Participant gains scope |
| Refuse | `acknowledgment` (response: refuse) | In Emergent Mode: `decision` (scope available for others). Otherwise signal-only. | No change |
| Relinquish | `boundary_change` | `boundary_change` (scope marked unowned) | Participant releases scope |
| Transfer | `handoff` + `acknowledgment` | `boundary_change` (release) + `boundary_change` (receipt) | Scope moves to recipient |
| Delegate | `handoff` | `attempt` (subtask by receiver) | Parent retained, subtask created |
| Converge | `query` (proposal) + `acknowledgment` (per participant) | `decision` (records participants, scope, conflict protocol) | Shared ownership begins |
| Diverge | `query` (proposal) + `acknowledgment` (per participant) | `decision` (records division of scope) | Shared ownership ends |

### Accept

A participant receives a task proposal and acknowledges it. Acceptance may be unconditional or with conditions (modified scope, additional resources, adjusted timeline). Acceptance is recorded as an `attempt` entry in the ledger (see `fnd-ledger.md`). The participant now holds ownership of the accepted scope.

### Refuse

A participant receives a task proposal and declines. A refusal returns: what was attempted (if anything), why it cannot proceed, and what kind of participant might succeed. Refusal is signal, not failure — it informs routing (see `fnd-signal.md`). In Orchestrated Mode, the refusal goes to the orchestrator as signal and they reroute — no ledger entry is required. In Emergent Mode, where there is no orchestrator to see the refusal, a refusal on an unowned or available scope produces a `decision` entry in the ledger so other participants can see the scope is available and self-select (see `fnd-field.md`).

### Relinquish

A participant releases scope it currently holds, without designating a specific recipient. This happens when a participant hits capacity limits, when work has drifted beyond its capability envelope, or when a participant recognizes that continued ownership no longer serves the coordination's intention.

A relinquish follows this protocol:

1. The participant writes the current state of work-in-progress to the ledger — what was done, what remains, what was learned, and any open questions. This is critical: without it, Recursion breaks for whoever picks up the scope (see `fnd-failure.md`).
2. A `boundary_change` entry is recorded, marking the scope as unowned.
3. The unowned scope surfaces as a signal to the coordination. In Orchestrated Mode, the orchestrator routes it. In Emergent Mode, participants self-select. In Infrastructure Mode, the orphaned scope is surfaced at the next mode transition (see `fnd-field.md`).

Relinquish is an act of Choice — recognizing the boundary of what one can influence and releasing what one cannot. It is not failure. A participant that holds scope beyond its capacity to serve it is doing more harm than one that lets go.

### Transfer

A participant hands scope to a specific recipient. Transfer requires:

1. The outgoing participant writes a state snapshot to the ledger — what was done, what remains, what was learned.
2. The incoming participant acknowledges receipt before assuming ownership.
3. Both the release and the acknowledgment are recorded as ledger entries.

Transfer is the Boundaries foundation in motion — ownership crosses a threshold, and the crossing is what gives the handoff its signal weight (see `fnd-signal.md`).

### Delegate

A participant holding scope proposes a subtask to another participant directly, without escalating to an orchestrator. The delegating participant retains ownership of the parent scope; the receiving participant takes ownership of the subtask scope.

Delegation is a signal envelope of type `handoff`. The receiving participant may accept, accept-with-conditions, or refuse — the same protocol as any task proposal. The delegation and its response are recorded in the ledger.

Delegation differs from orchestration: it is peer-to-peer, scoped to a specific subtask, and does not require a mode transition. It is how work naturally subdivides in Emergent Mode without requiring someone to step into the orchestrator role.

### Converge

Multiple participants agree to share ownership of the same scope and work collaboratively. Convergence is the appropriate pattern when a scope requires perspectives, skills, or capacities that no single participant holds — and when decomposing the scope into independent subscopes would lose the integration that makes it coherent.

Convergence follows this protocol:

1. A participant proposes convergence on a specific scope, identifying the participants being invited and the reason shared ownership serves the work.
2. Each invited participant accepts or refuses. Convergence requires consent from all converging participants — no one is absorbed into shared scope involuntarily.
3. A `decision` entry is recorded in the ledger identifying the converging participants, the shared scope, and the conflict protocol they will follow for concurrent work within that scope.
4. While converged, participants coordinate signal within the shared scope. The conflict protocol they declared governs how disagreements are resolved — this may be consensus, designated tiebreaker, or escalation to a human.

Convergence has a higher signal cost than independent ownership — participants must attend to each other's work within the scope, not just the ledger. This cost is tracked under Balance (see preamble). If the cost exceeds the value of integration, that is a signal to diverge.

### Diverge

Participants in a converged scope return to independent ownership. Divergence may occur because the shared work is complete, because the cost of convergence exceeds its value, or because the scope has clarified enough to decompose into independent parts.

Divergence follows this protocol:

1. A participant proposes divergence, identifying how the shared scope will be divided or who retains ownership of what remains.
2. Converging participants acknowledge the proposed division.
3. A `decision` entry is recorded in the ledger, closing the convergence and establishing the new ownership boundaries.

Divergence without recording the state of the shared work is a Recursion violation — the same as relinquish without a ledger write. The convergence's learning must survive the divergence.

---

## Exit States

### Graceful Departure

A participant leaves the coordination intentionally. Graceful departure follows this protocol:

1. The participant relinquishes all held scope (following the relinquish protocol above for each).
2. The participant transitions any active convergences through the diverge protocol.
3. A `boundary_change` entry is recorded marking the participant as departed. The participant is removed from the Active Participants table but remains in the Coordination Directory with their full history.
4. Departure is not removal — the coordination's memory of who participated and what they contributed is part of Recursion.

### Ungraceful Departure

A participant becomes unresponsive without notice — an API outage, a crashed process, a human who disappears. The Timeout circuit breaker will fire when signals to the participant go unacknowledged past their declared latency tolerance (see preamble).

When ungraceful departure is detected:

1. The breach is handled through the failure and repair cycle (see `fnd-failure.md` → `fnd-repair.md`).
2. All scope held by the departed participant is marked as orphaned. Any active convergences involving the departed participant are flagged to remaining converging participants, who must decide: continue the convergence with reduced participation (recording the changed composition as a `decision` entry), or diverge (following the diverge protocol). A convergence cannot silently lose a participant — the remaining participants must acknowledge the changed conditions.
3. A `failure` entry is recorded with `foundation_tag: ["boundaries", "recursion"]` — boundaries because ownership is now ambiguous, recursion because work-in-progress may not have been committed to the ledger.
4. The orphaned scope is routed according to the current field mode (see `fnd-field.md`).
5. The participant is marked as unresponsive in the Active Participants table. They are not removed until departure is confirmed or the participant returns.

The departed participant is not permanently excluded. If the underlying cause is resolved (API restored, process restarted), the participant may re-enter through the return protocol.

### Return

A participant who previously departed re-enters the coordination. Return requires a fresh declaration — the participant's capabilities, boundaries, and constraints may have changed since departure.

The coordination's memory of the participant persists in the Coordination Directory and the ledger. Prior contributions, failures, and repairs associated with the returning participant are visible to all participants. Trust is not reset to zero, but it is not automatically restored to its prior level — it is recalibrated based on the full ledger history.

If the participant departed ungracefully, the return should include a signal acknowledging what happened — consistent with the repair principle of demonstrated change (see `fnd-repair.md`). This is not a punishment gate; it is a trust restoration practice.
