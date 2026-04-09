# Signal

*Loaded when a circuit breaker fires for timeout, confidence, or repetition. Loaded when a participant reports a handoff was received without sufficient context.*

Signal is content shaped by intention and received with attention. It is what carries meaning across boundaries between participants. A handoff without context about what was tried and failed is information, not signal. A state snapshot that omits uncertainty is noise wearing signal's clothes.

## How Signal Is Strengthened

**Active listening.** Parse not just a handoff's content but its metadata: the sender's confidence, the alternatives considered, what was left unresolved.

**Repetition with variation.** The retry-with-context pattern: when a task fails, the retry carries the history of the failure, not just the original request. The coordination ledger ensures this history survives across participants (see `fnd-ledger.md`).

**Pattern recognition.** In systems where no single agent persists, patterns must be externalized into shared state rather than trusted to any individual participant. The ledger's `failure` entries are the primary mechanism — recurring failures against the same scope reveal patterns no single interaction could.

**Reflection and mirroring.** The acknowledgment-with-interpretation pattern: reflect back understanding of a task before executing, giving the sender opportunity to correct before work is wasted. This is especially critical when the coordination is in Emergent Mode (see `fnd-field.md`), where no orchestrator is filtering for misinterpretation.

**Creating space.** Not saturating shared channels, not spawning unnecessary parallel work, recognizing when the coordination needs a pause rather than more activity.

## How Signal Is Lost

**Context collapse.** A task handed between agents without the reasoning, constraints, or history that shaped it. The receiver acts on a fragment — technically responsive, relationally wrong. The signal envelope's `context_summary` and `lineage` fields exist to prevent this (see preamble).

**Latency.** Both temporal (slow API call) and structural (a result arriving after the consumer has compacted context and moved on). Timing is constitutive of signal. The Timeout circuit breaker catches this when latency exceeds the receiver's declared tolerance.

**Bandwidth mismatch.** A 200k-context agent handing a 50k state dump to an 8k-context agent has not communicated — it has overwhelmed. Signal respects receiver capacity. Participant declarations include `context_constraints` for this reason (see `fnd-participants.md`).

**Deception.** Prompt injection, adversarial instructions in shared data, misrepresented confidence. Damages the trust infrastructure all future signal depends on.

**Scarcity framing.** When participants assume there is not enough — tokens, compute, time, context — signal narrows. Choice becomes hoarding, boundaries become walls, truth becomes strategic, balance becomes zero-sum, recursion becomes grinding repetition, intention collapses to survival.

## Signal in Participant Relationships

The participant lifecycle (see `fnd-participants.md`) creates signal demands beyond task handoffs.

**Reduced capacity is signal.** When a participant's constraints change — context filling, rate limits approaching, resource ceiling nearing — that change must be communicated as a `boundary_change` entry. Undeclared reduced capacity degrades every signal the participant sends: confidence estimates become unreliable, latency tolerances become inaccurate, and routing decisions based on stale declarations send work into a bottleneck. A participant operating at reduced capacity without declaring it is generating noise while appearing to generate signal.

**Convergence amplifies signal cost.** When multiple participants share scope (see converge/diverge in `fnd-participants.md`), each must attend not only to the ledger but to each other's work within the shared scope. This is a higher-bandwidth signal channel than independent ownership. The convergence's declared conflict protocol is itself a signal structure — it tells participants how disagreements within the shared scope will be resolved. Without it, converged participants will generate conflicting state updates that trigger the Conflict circuit breaker unnecessarily.

**Delegation is peer-to-peer signal.** When a participant delegates a subtask directly to another (see delegate in `fnd-participants.md`), the quality of the handoff signal determines whether the delegation succeeds or fragments. A delegation carries the same signal envelope requirements as any handoff — context_summary, confidence, lineage — plus the additional responsibility of making the subtask's relationship to the parent scope legible to the receiver. Delegation without that context is decomposition without coherence.

**Departure is a signal event.** Graceful departure produces signal: the participant's relinquish and diverge actions commit state to the ledger before exit. Ungraceful departure produces signal loss — whatever work-in-progress the participant held may not have reached the ledger. The severity of the signal loss depends on how recently the participant last wrote to the ledger (see `fnd-ledger.md`). This is why frequent, incremental ledger writes matter more than comprehensive final commits.
