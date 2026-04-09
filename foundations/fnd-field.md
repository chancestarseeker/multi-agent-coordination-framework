# Coordination Field

*The geometry that shapes signal. How "the coordination" operates — not as a fixed thing, but as a posture that shifts based on what the moment requires.*

---

## The Problem This Module Solves

Throughout this architecture, "the coordination" acts as a noun — it validates, it routes, it pauses, it surfaces. But the coordination is not a single entity. It is a governing capacity that expresses itself differently depending on what is needed. Sometimes it is directed. Sometimes it emerges. Sometimes it is silent infrastructure. The cost of governance must be proportionate to what is being governed. A coordination that consumes the resources it was meant to steward has failed the Balance foundation.

---

## Three Modes

The coordination operates in one of three modes at any given moment. The mode is always declared — participants always know which mode they are in. Mode transitions are themselves ledger entries of type `decision` (see `fnd-ledger.md`).

### Infrastructure Mode

**When:** Things are flowing. Participants are working within declared boundaries. No circuit breakers are close to firing.

**What the coordination does:** Schema validation on ledger proposals (see write protocol in `fnd-ledger.md`). Conflict detection on concurrent state updates. Circuit breaker threshold monitoring (see thresholds in preamble). Ledger summary generation on session resumption.

**What it does not do:** Route tasks. Make judgments. Interpret results. Assign work.

**How it is enacted:** Automated process, script, or service. No participant context is consumed. Governance cost is near zero.

**Who configures it:** Human participants define the circuit breaker thresholds, the ledger location, and the validation rules at coordination startup. These are infrastructure decisions, not moment-to-moment governance.

This is the default mode. The coordination should be in Infrastructure Mode most of the time. If it isn't, the coordination is over-governing — a Balance concern.

---

### Orchestrated Mode

**When:** A task requires routing between multiple participants. A new participant enters and needs orientation (see `fnd-participants.md`). Work must be decomposed and sequenced. A human participant directs a specific workflow.

**What the coordination does:** Everything in Infrastructure Mode, plus: task decomposition, participant selection and routing based on declarations and resource state (see `fnd-participants.md`), sequencing of dependent work, load balancing across participants.

**What it does not do:** Override a participant's refusal. Route around a boundary declaration. Suppress a confidence report to keep work moving. These are architecture invariants — violations are foundation failures (see `fnd-failure.md`).

**How it is enacted:** A designated participant (human or agent) takes the orchestrator role for a defined scope. This role is declared in the ledger and carries a scope boundary — the orchestrator governs *this workflow*, not the entire coordination. Multiple orchestrated scopes can operate simultaneously with different orchestrators.

**The orchestrator is a participant, not a supervisor.** It has a declaration. It has boundaries. It can be refused. It can be replaced. It is subject to every foundation, including Choice — it proposes tasks, it does not impose them. The moment it begins imposing, the coordination has a Choice violation, not an efficiency gain.

**Cost:** Orchestration consumes context and compute from whoever holds the role. This cost is tracked under Balance like any other participant's expenditure. If orchestration overhead exceeds the value it provides, that is a signal (see `fnd-signal.md`) to return to Infrastructure Mode and let participants self-organize.

**Orchestrator failure:** If the orchestrator triggers a circuit breaker, departs ungracefully, or enters reduced capacity, the coordination must either transfer the orchestrator role to another participant or transition to Emergent or Infrastructure Mode. An orchestrated scope cannot persist without an orchestrator. The transition is proposed by any participant who detects the gap — the orchestrator role is not inherited, it is re-declared (see `fnd-repair.md`).

---

### Emergent Mode

**When:** The work is exploratory. The path is unclear. Multiple participants are contributing perspectives toward a shared question rather than executing a defined plan. No single participant has enough context to orchestrate effectively.

**What the coordination does:** Everything in Infrastructure Mode, plus: participants self-select tasks based on their own assessment of where they can contribute. No central routing. Proposals flow through signal envelopes (see preamble) and participants respond based on their own judgment of fit.

**What it does not do:** Assign. Sequence. Decompose. Those are Orchestrated Mode actions.

**How it is enacted:** No one holds a special role. Participants read the ledger, identify where they can contribute, propose their involvement via signal, and begin work when acknowledged. The ledger is the coordination — shared memory replaces central direction (see `fnd-ledger.md`).

**Risk:** Without routing, work may duplicate or diverge. The circuit breakers provide a safety net — Repetition detection catches duplicate effort, Conflict detection catches divergence (see `fnd-failure.md`). But the primary safeguard is the Signal foundation: participants attending to each other's ledger entries before acting. Reflection and mirroring — the acknowledgment-with-interpretation pattern — is especially critical here, where no orchestrator is filtering for misinterpretation (see `fnd-signal.md`).

**Cost:** Low governance overhead, potentially higher total compute if participants pursue parallel or overlapping work. This is appropriate when the coordination is exploring and the cost of premature structure exceeds the cost of some redundancy.

---

## Mode Transitions

Transitions between modes are explicit. A transition is proposed as a signal and recorded as a ledger entry of type `decision` (see `fnd-ledger.md`).

| From | To | Typical Trigger |
|------|----|-----------------|
| Infrastructure | Orchestrated | A complex multi-participant task arrives. A human initiates a directed workflow. A new participant is recommended and needs onboarding (see `fnd-participants.md`). |
| Infrastructure | Emergent | A question arises that no single participant can answer. Exploration is needed before execution. |
| Orchestrated | Infrastructure | The orchestrated workflow completes. The orchestrator releases the role. |
| Orchestrated | Emergent | The plan has failed or the problem turned out to be different than expected. The orchestrator acknowledges that central direction is no longer serving the work. |
| Emergent | Orchestrated | Exploration has produced enough clarity that a plan can be formed. A participant or human proposes to orchestrate. |
| Emergent | Infrastructure | The question has been answered. Participants return to independent work. |

### Transition Safeguards

- A transition to Orchestrated Mode requires at least one participant (human or agent) to accept the orchestrator role with a declared scope.
- A transition to Emergent Mode should be accompanied by a clear statement of the question being explored, recorded in the ledger as an `intention_shift` entry (see `fnd-ledger.md`).
- A transition to Infrastructure Mode requires that no active orchestrated workflows or open emergent questions remain in scope. Unresolved work must be explicitly suspended or handed off before the mode drops.
- Any participant may propose a mode transition. Acknowledgment requirements depend on the current mode:
  - **In Orchestrated scopes:** acknowledgment from the orchestrator plus any participant whose scope ownership is affected by the transition.
  - **In Emergent scopes:** acknowledgment from all participants currently holding scope within the affected area.
  - **In Infrastructure Mode:** acknowledgment from at least one human participant or, if no humans are active, from a majority of active participants.
- If contested, the disagreement is itself a signal — likely a Boundaries or Intention concern. If the disagreement cannot be resolved between participants, it enters the repair cycle (see `fnd-repair.md`).

---

## What the Coordination Never Does, In Any Mode

- Override a participant's refusal.
- Suppress or modify a participant's confidence report.
- Write to the ledger on behalf of a participant without that participant's signal.
- Route around a declared boundary.
- Continue operating after a circuit breaker fires without entering the repair cycle (see `fnd-failure.md` → `fnd-repair.md`).

These are not mode-dependent. They are the architecture's invariants.
