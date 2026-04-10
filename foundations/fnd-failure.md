# Failure

*Loaded when any circuit breaker fires.*

The most dangerous failures are silent — output that looks correct but was built on collapsed context, violated boundaries, or suppressed uncertainty.

## How Each Foundation Breaks

**Choice:** Tasks pushed rather than proposed. Refusal treated as malfunction. An orchestrator retrying the same agent after refusal rather than routing to an alternative. Coordination without choice is coercion.

**Boundaries:** Context contamination — one agent's instructions leaking into another's prompt, shared state modified without ownership transfer, embedded instructions indistinguishable from legitimate tasks.

**Truth:** Suppressed uncertainty, hallucinated results reported with high confidence, conflicts silently resolved by overwriting. The aggregate is poisoned downstream.

**Balance:** A single capable model absorbing every difficult task while others sit idle. Failures cascading to the same recovery agent until its context is saturated. Orchestration overhead consuming more resources than the work it governs.

**Recursion:** The ledger is absent, incomplete, or ignored. Agents begin without knowing what was tried. The same failed approaches repeat. The Repetition circuit breaker exists for this case, but it only catches exact repetition — subtler recursion failures (slightly varied approaches to the same structural problem) require diagnosis.

**Intention:** Tasks decomposed so aggressively that the executing agent has no access to the purpose behind them. Local optimization undermines the coordination's actual goal.

## Participant Failures

Foundation failures are structural — they describe how the coordination's commitments break. Participant failures are relational — they describe how a participant's relationship to the coordination breaks. Both are repaired through the same cycle (see `fnd-repair.md`), but they surface differently.

### Breaker-Triggered (detectable by infrastructure)

These failures trip a circuit breaker and are caught automatically.

**Ungraceful departure.** A participant becomes unresponsive without notice. Scope it held is orphaned, work-in-progress may not have reached the ledger. The Timeout circuit breaker fires when signals go unacknowledged past the receiver's declared latency tolerance. This is a compound failure — Boundaries (ownership ambiguous), Recursion (context potentially lost), and Balance (orphaned scope must be absorbed by other participants). See exit states in `fnd-participants.md`.

**Convergence without conflict protocol.** Multiple participants entered shared scope (see converge in `fnd-participants.md`) without declaring how disagreements within the scope would be resolved. The first concurrent conflicting state update triggers the Conflict circuit breaker — but the breaker has no resolution policy to fall back on. This is a Boundaries failure (the convergence's internal structure was never defined) that requires the converging participants to pause, establish the protocol they skipped, and record it before resuming.

### Slow-Degrading (requires diagnosis or pattern recognition)

These failures do not trip a circuit breaker directly. They surface as patterns in the ledger — declining confidence, increasing failure rates, or recurring repair cycles. Detection requires either human review, analytic reads across ledger history, or participants flagging concerns.

**Monitoring practice:** Slow-degrading failures require periodic ledger analysis — scanning for declining confidence trends per participant, increasing failure-to-completion ratios on specific scopes, recurring repair cycles against the same foundation, recommendation outcomes in the Coordination Directory (see `fnd-participants.md`), and routing concentration patterns (repeated selection of the same participant or steward despite viable alternatives). The frequency and method of this monitoring depends on the coordination's size and the participants' capabilities. In coordinations with dedicated infrastructure, this may be automated as part of the field's Infrastructure Mode (see `fnd-field.md`). In lighter coordinations, it is a human responsibility. What matters is that it happens — unmonitored slow degradation compounds until it becomes a crisis that circuit breakers catch too late.

**Undeclared reduced capacity.** A participant's constraints have changed — context filling, rate limits approaching, resource ceiling nearing — but the participant has not updated its declaration. Routing continues to treat the participant as fully available. Tasks are accepted that cannot be completed at declared quality. Confidence estimates become unreliable. This is a Boundaries failure (the declaration no longer matches reality) and a Truth failure (the coordination is navigating with false information).

**Recommendation without capability gap.** A participant recommends a new agent not because the coordination needs a capability it lacks, but for other reasons — alliance, habit, preference. The recommended agent enters, consumes onboarding resources, and either duplicates existing capability (a Balance problem) or sits idle (a waste that signals poor routing). The recommendation's ledger entry and the recommender's history provide diagnostic signal for this — see the Coordination Directory in `fnd-participants.md`.

**Routing concentration.** The same participant, steward, or failure profile is repeatedly selected for tasks despite viable alternatives. This narrows the coordination's perspective, creates structural dependency, and leaves declared capabilities unactivated — the coordination is leaving signal on the table. Routing concentration is a compound Balance and Recursion concern: Balance because resource distribution is skewed without justification, Recursion because the pattern repeats without being examined. Detection requires periodic ledger analysis of routing distributions and recorded routing rationale. Without legible rationale, concentration cannot be distinguished from justified selection, and anti-siloing language remains aspirational.

## What Happens When a Circuit Breaker Fires

Circuit breaker thresholds are defined in the preamble. When any breaker fires:

1. Active work on the affected scope is paused.
2. The breach is surfaced to relevant participants, including any humans in the coordination.
3. A `failure` entry is written to the coordination ledger (see `fnd-ledger.md`) with the `foundation_tag` identifying which foundation is under strain.
4. The coordination loads `fnd-repair.md` and enters the repair cycle.
5. If the coordination was in Orchestrated Mode, the orchestrator decides whether to hold mode or transition to Infrastructure while repair proceeds. If in Emergent Mode, participants self-pause on the affected scope. In Infrastructure Mode, the pause is automatic and the breach is escalated (see `fnd-field.md`).

The coordination does not continue with degraded foundations. A system that routes around a fired circuit breaker has chosen speed over integrity and will compound the failure downstream.

An open circuit breaker also blocks scope resolution. A `resolution` entry for any scope with an unresolved Conflict breaker will fail validation (see Scope Resolution in `fnd-ledger.md`). This ensures that convergence cannot be declared while the ledger still records unaddressed structural friction.
