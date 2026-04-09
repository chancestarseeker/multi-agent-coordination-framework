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
| **type** | enum | `decision` · `attempt` · `completion` · `failure` · `repair` · `boundary_change` · `intention_shift` |
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

## Maintenance

**The ledger does not self-compact.** Compaction is a lossy operation and the ledger is the coordination's memory. However, the summary generation process serves the same function without data loss — the full ledger is preserved, but participants interact with compressed views.

**Stale entries are never deleted.** An entry that is superseded by a later correction remains in the ledger. The correction references it via `prior_entries`. This preserves the coordination's learning history — knowing what was once believed and why it was revised is itself valuable signal.

**Ledger health is a Balance concern.** If the ledger is growing faster than summaries can compress it, or if participants are spending disproportionate context on ledger reads, the coordination should surface this as a resource imbalance.

**Frequent, incremental writes over comprehensive final commits.** A participant that writes to the ledger only upon completion risks total signal loss if it departs ungracefully (see `fnd-failure.md`). Intermediate writes — after meaningful progress, before risky operations, at natural breakpoints — reduce the damage window. The severity of ungraceful departure is directly proportional to how long ago the participant last wrote to the ledger (see `fnd-signal.md`).
