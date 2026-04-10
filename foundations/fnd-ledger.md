# Coordination Ledger

*The shared memory of the coordination. Every participant reads from it. Every participant proposes to it. No participant owns it.*

---

## What the Ledger Is

The ledger is an append-only record of state transitions in the coordination. It is not a log — logs record events; the ledger records **what changed, why, and what it means for what comes next**. Each entry carries enough context that a participant encountering it for the first time can orient without loading the full history.

The ledger is the mechanism by which Recursion survives the absence of individual memory.

## What the Ledger Is Not

It is not a task queue, a message bus, or a database. Tasks are routed through signal envelopes. The ledger records the *outcomes* of those signals — what was decided, attempted, completed, failed, or learned. If it starts being used to assign work, it has drifted from its purpose.

---

## Entry Schema

Every ledger entry contains these fields:

| Field | Type | Content |
|-------|------|---------|
| **entry_id** | string | Unique identifier for this entry. |
| **timestamp** | ISO 8601 | When the entry was written. |
| **author** | string | Identifier of the participant proposing this entry. |
| **type** | enum | `decision` · `attempt` · `completion` · `failure` · `repair` · `boundary_change` · `intention_shift` · `resolution` · `objection` · `withdrawal` · `reopen` |
| **scope** | string | Which artifact, task, or coordination area this entry concerns. |
| **prior_entries** | string[] | Entry IDs this one depends on or responds to. Empty for root entries. |
| **summary** | string | One to three sentences: what happened and why it matters. A new participant should be able to read this field alone and orient. |
| **detail** | string | Full context — reasoning, alternatives considered, constraints encountered. May be omitted from ledger summaries. |
| **confidence** | float (0.0–1.0) | Author's honest estimate of the reliability of this entry's content. |
| **foundation_tag** | string[] | Which foundations are relevant to this entry (e.g., `["truth", "balance"]`). Used for diagnosis when a circuit breaker fires. |

### Entry Types

**decision** — A choice was made. The entry records what was chosen, what alternatives were considered, and why this option was selected. Links to the intention it serves. In participant lifecycle terms: recommendations for new participants, convergence proposals, divergence proposals, field mode transitions, and self-proposals are all `decision` entries — each tagged with the relevant foundations (e.g., `["choice", "boundaries"]` for a recommendation that expands the coordination's participant ecology). See `fnd-participants.md` and `fnd-field.md`.

**attempt** — Work was begun. Records the task, the participant undertaking it, the approach being taken, and any conditions or constraints declared at the start. Written when a participant accepts a task, accepts a delegated subtask, or picks up an orphaned scope after relinquish. See scope actions in `fnd-participants.md`.

**completion** — Work finished. Records what was produced, the participant's confidence in the result, and any open questions or known limitations.

**failure** — Work did not succeed. Records what was tried, why it failed, and what the participant learned. This is the most important entry type for preventing recursive failure loops. Also written when a circuit breaker fires (see `fnd-failure.md`) and when ungraceful departure is detected — tagged with `["boundaries", "recursion"]` because ownership becomes ambiguous and work-in-progress may be lost. See exit states in `fnd-participants.md`.

**repair** — A conflict was resolved. Records the breach, the diagnosis, the resolution applied, and whether prior failures contributed. Written at the end of a repair cycle (see `fnd-repair.md`). Links to the `failure` entry that initiated the cycle via `prior_entries`. Resolutions may include participant scope actions — relinquish, transfer, diverge, declaration update, or departure.

**boundary_change** — A participant's relationship to the coordination has changed. This covers: new participant entry (declaration filed), declaration updates (capability, constraints, or availability changed), reduced capacity (constraints tightened beyond original declaration), relinquish (scope marked as unowned), graceful departure (participant exited), ungraceful departure detected (participant marked unresponsive), and return (fresh declaration from a previously departed participant). Other participants should recalibrate expectations on every `boundary_change`. See `fnd-participants.md`.

**intention_shift** — The coordination's purpose has changed. All active participants should re-evaluate whether their current work still serves the new intention. Also written when a transition to Emergent Mode is accompanied by a statement of the question being explored (see `fnd-field.md`).

**resolution** — A scope has converged. Any participant currently active in the scope may propose a resolution. The entry references the verdict, repair, and other entry IDs that constitute the convergence (the author is showing their work, pointing at ledger evidence) and includes a natural language summary of what was converged on. Infrastructure validates the proposal before accepting it — see Scope Resolution below.

**objection** — A participant formally blocks resolution of a scope. The entry names the scope, references the specific entries being objected to, and provides a reason. An active objection prevents any `resolution` entry for that scope from passing validation. Objections remain active until withdrawn by their author or addressed through a completed repair cycle. Any participant currently active in the scope may write an objection.

**withdrawal** — The author of a prior `objection` retracts it. References the objection entry being withdrawn, with an optional reason. Only the original objection's author may write a withdrawal for it. Clearing an objection via withdrawal removes it as a blocker to resolution.

**reopen** — A previously resolved scope is returned to active status. Any participant currently active in the scope may write a reopen entry, referencing the `resolution` entry being reopened and providing a reason. Once reopened, the scope accepts new verdicts, objections, and eventually a new resolution.

---

## Where the Ledger Lives

The ledger is stored in a location accessible to all participants but owned by none. It must be:

- **Readable** by every participant in the coordination.
- **Appendable** by every participant (via proposal — see Write Protocol below).
- **Immutable** once written. Entries are never edited or deleted. Corrections are new entries that reference and supersede prior ones.
- **Durable** across sessions. The ledger survives participant restarts, context window resets, and session boundaries.

Implementation will vary by coordination environment. A shared filesystem, a version-controlled repository, a database, or a dedicated coordination service are all valid — provided they meet the four requirements above. The simplest viable implementation is a single append-only file in a shared directory.

---

## Write Protocol

Participants do not write directly to the ledger. They **propose** entries.

1. Participant constructs an entry conforming to the schema above.
2. Participant submits the entry as a `state_update` signal (via the signal envelope) to the coordination.
3. The coordination validates the entry: schema compliance, author matches the signal origin, prior_entries reference existing entries. In Infrastructure Mode, this validation is automated. In Orchestrated or Emergent Mode, the same validation applies — the field mode determines *who governs*, not whether validation occurs (see `fnd-field.md`).
4. If valid, the entry is appended. If invalid, the proposing participant receives an `error` signal describing what failed validation.
5. If two entries propose incompatible state for the same scope (a conflict), neither is appended. Both proposing participants receive a `conflict` notification. This triggers the Conflict circuit breaker.

There is no approval queue. Valid, non-conflicting entries are appended immediately. The goal is low-latency shared memory, not gatekeeping.

---

## Read Protocol

### Full Ledger
Any participant may read the complete ledger at any time. For short coordinations, this is sufficient.

### Ledger Summary
For coordinations with large histories, participants receive a **summary** on entry or session resumption. The summary is generated by compressing the ledger into a condensed representation:

- Include the `summary` field of every entry. Omit `detail`.
- Preserve all `failure` and `repair` entries in full — these carry the highest signal density for preventing repeated mistakes.
- Preserve all `intention_shift` and `boundary_change` entries in full — these define current operating conditions.
- For `decision`, `attempt`, and `completion` entries, include only those relevant to currently active scope. Completed and closed scopes may be compressed to a single summary line.

The summary should be small enough to fit within the smallest context window of any active participant. If it is not, the coordination has grown beyond what its current participants can hold, and that itself is a signal (a Balance concern).

The Coordination Directory (see `fnd-participants.md`) is also built from ledger reads — it aggregates `completion`, `failure`, and `repair` entries per participant to construct trust signal over time. The directory is a derived view of the ledger, not a separate data store.

### Scoped Reads
A participant working on a specific task may request only ledger entries matching a given `scope` value. This is the most context-efficient read pattern for focused work.

---

## Scope Resolution

A scope begins as **active** when the first entry referencing it is written. It remains active until a `resolution` entry passes validation and is appended, at which point the scope becomes **resolved**. A `reopen` entry returns a resolved scope to active.

```
[active] --(resolution validated and appended)--> [resolved]
[resolved] --(reopen appended)--> [active]
```

### Resolution Validation

Infrastructure validates every proposed `resolution` entry before accepting it. The validation checks:

1. **No open conflict breakers** for the scope. If a Conflict circuit breaker has fired (see `fnd-failure.md`) and no completed `repair` entry references that failure, the resolution is rejected.
2. **No active objections** for the scope. An objection is active if no `withdrawal` from the same author references it and no completed `repair` entry references it as addressed. If any active objection exists, the resolution is rejected.
3. **At least one verdict exists** for the scope. An empty scope cannot be resolved.

When validation fails, the rejection names exactly what is blocking — which unresolved conflict, which active objection — so the proposing participant knows what must be addressed before resolution can proceed.

### Resolution Is Not a Vote

There is no quorum. There is no counting. Resolution is a claim that the ledger state supports closure, validated by infrastructure against the conditions above. Convergence is the absence of unresolved friction — not the presence of sufficient agreement.

All active participants have equal standing to propose resolution or raise objections. The resolution mechanism does not rank, score, or weight participants in any way.

### Resolved Scope Behavior

Once a scope is resolved, new verdicts and attempts targeting that scope are rejected by infrastructure. The resolution entry becomes the canonical reference point — any participant asking "what happened with this scope?" reads the resolution and follows its references back into the ledger.

If circumstances change or new information surfaces, any active participant may write a `reopen` entry, which returns the scope to active and allows the full cycle to proceed again.

---

## Maintenance

**The ledger does not self-compact.** Compaction is a lossy operation and the ledger is the coordination's memory. However, the summary generation process serves the same function without data loss — the full ledger is preserved, but participants interact with compressed views.

**Stale entries are never deleted.** An entry that is superseded by a later correction remains in the ledger. The correction references it via `prior_entries`. This preserves the coordination's learning history — knowing what was once believed and why it was revised is itself valuable signal.

**Ledger health is a Balance concern.** If the ledger is growing faster than summaries can compress it, or if participants are spending disproportionate context on ledger reads, the coordination should surface this as a resource imbalance.

**Frequent, incremental writes over comprehensive final commits.** A participant that writes to the ledger only upon completion risks total signal loss if it departs ungracefully (see `fnd-failure.md`). Intermediate writes — after meaningful progress, before risky operations, at natural breakpoints — reduce the damage window. The severity of ungraceful departure is directly proportional to how long ago the participant last wrote to the ledger (see `fnd-signal.md`).
