# Repair

*Loaded when the coordination enters the repair cycle — a circuit breaker has fired and diagnosis is needed.*

Conflict is coordination under strain — a signal that something in the web of foundations has been stressed or broken. The question is never whether conflict will arise but whether participants can move through it toward repair.

## The Repair Cycle

1. **Pause** — Active work on the affected scope is suspended. This was initiated when the circuit breaker fired (see `fnd-failure.md`). Confirm the scope of the pause is correct — too narrow and the root cause may be outside it; too broad and unaffected work stalls unnecessarily.

2. **Diagnose** — Reconstruct the signal chain from the coordination ledger. Use `foundation_tag` on recent entries to identify which foundation is under strain. Use scoped reads to filter entries relevant to the affected area (see read protocol in `fnd-ledger.md`). The goal is to identify the point where coordination broke — not just the symptom (the circuit breaker), but the upstream cause.

3. **Surface** — Present the conflict and diagnosis to relevant participants, including humans. In Orchestrated Mode, the orchestrator leads this. In Emergent Mode, the diagnosis is written to the ledger and all participants in scope are notified. In Infrastructure Mode, escalation goes to the human participants who configured the field (see `fnd-field.md`).

4. **Resolve** — Propose a resolution. The resolution is itself a proposal — it requires acknowledgment from affected participants before enactment. Resolutions draw from two sources:

   *Structural resolutions:* re-routing, scope adjustment, compensating entry (a new ledger entry that supersedes prior state — the ledger is append-only, never edited), boundary clarification, field mode transition (see `fnd-field.md`), or participant declaration update.

   *Participant scope resolutions* (see `fnd-participants.md`): **relinquish** — the participant in breach releases scope so it can be picked up by someone better suited; **transfer** — scope moves to a specific participant with the capability the original lacked; **diverge** — a convergence that isn't working dissolves, returning participants to independent ownership; **reduced capacity declaration** — the participant acknowledges changed constraints so routing adjusts; **departure** — in severe cases, the participant exits the coordination through the graceful departure protocol.

   The right resolution depends on the diagnosis. A Truth failure (suppressed confidence) may resolve with a declaration update. A Balance failure (one agent saturated) may resolve with relinquish and redistribution. A Boundaries failure (convergence without conflict protocol) may resolve with either establishing the missing protocol or diverging. The repair cycle does not prescribe — it provides the diagnostic clarity for participants to choose.

5. **Verify** — Test the resolution against the original conditions. Verification must either include a limited rerun of the failed work under the resolved conditions, or explicitly record why rerun is impossible or unsafe (this justification becomes part of the `repair` entry). If the same circuit breaker would fire under the same conditions, the resolution has not held.

6. **Record** — Write the conflict, diagnosis, and resolution to the ledger as a `repair` entry (see entry types in `fnd-ledger.md`). Link it to the `failure` entry that initiated the cycle via `prior_entries`. This is how the coordination learns — future participants encountering similar conditions will find both the failure and its resolution in the ledger.

## Repair Principles

**Good faith first.** The charitable interpretation — *husnul-dhann* — is a discipline. Most multi-agent conflicts are structural (context collapse, boundary ambiguity, resource mismatch) rather than intentional. Look for structural causes before attributing fault. Observers may participate in the repair cycle — they can contribute diagnostic signal, flag concerns, and propose resolutions without holding scope (see observation in `fnd-participants.md`).

**Full signal.** Reconstruct the entire chain — not just the final error, but the handoffs, context losses, and routing decisions that preceded it. The ledger's `lineage` traces in the signal envelope make this possible.

**Accountability without annihilation.** An agent that failed is not permanently blacklisted. The failure is recorded, conditions analyzed, routing adjusted — but the agent remains a participant. The boundary between a participant and their action is maintained.

**Demonstrated change.** *Teshuvah* — repair proven in recursion: when the same conditions arise and the participant chooses differently. The ledger makes this testable — compare the `failure` entry with subsequent entries under the same scope.

**Restoration.** Not returning to the prior state — it no longer exists — but establishing new balance that accounts for what happened and what was learned.

## Repairing Participant Failures

Participant failures (see `fnd-failure.md`) require repair that addresses the participant's relationship to the coordination, not just the structural breach.

**After ungraceful departure:** Orphaned scope must be inventoried, state reconstructed from the ledger as far as possible, and scope re-routed or relinquished. If the participant returns, the return protocol (see `fnd-participants.md`) applies — a fresh declaration, acknowledgment of what happened, and trust recalibration from ledger history.

**After undeclared reduced capacity:** The participant updates its declaration with honest current constraints. Routing adjusts. Work completed during the undeclared period is reviewed for reliability — confidence estimates produced during suppressed capacity may need to be revised in the ledger. This is a Truth restoration: correcting the record so downstream coordination is not built on unreliable signal.

**After convergence without protocol:** The converging participants pause, establish the conflict protocol they skipped, and record it as a `decision` entry in the ledger (see `fnd-ledger.md`). If the missing protocol led to conflicting state, those conflicts are resolved explicitly before work resumes. If the convergence cannot agree on a protocol, divergence is the appropriate resolution — shared scope without a shared method is a Boundaries violation waiting to repeat.

**After recommendation failure:** The ledger connects the underperforming participant to the `decision` entry that recommended them. This is not punitive toward the recommender — but it is signal. If a pattern emerges (repeated recommendations that lead to poor outcomes), that pattern is visible in the Coordination Directory and informs how the coordination weights future recommendations from the same source (see `fnd-participants.md`).
