# Coordination — Setup & Run

Minimal Git+LiteLLM loop implementing §7 of `../coordination-tech-stack.md`.
What lives here:

```
coordination/
├── orchestrator.py            # The minimal review loop
├── config.json                # Mode, intention, circuit breaker thresholds
├── requirements.txt
├── foundations/               # fnd-*.md from chancestarseeker/multi-agent-coordination-framework
├── participants/declarations/ # claude-sonnet, gpt-4o, human-lead
├── scope/                     # Artifacts under review (one example placed in scope/code/)
├── ledger/entries/            # Append-only review entries (git-committed)
└── signal/                    # Reserved for inbox/archive in a later iteration
```

## 1. Install

```bash
cd coordination
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Provide API keys

LiteLLM reads provider keys from environment variables. For the two default
agents you need at minimum:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
```

If you only have one of the two, delete (or rename out) the corresponding
`participants/declarations/*.json` file before running — the orchestrator
calls every active agent it finds.

## 3. Run a review

```bash
python orchestrator.py review --scope scope/code/example_auth.py
```

Each active agent receives:
- its own declaration
- the loaded foundations (`fnd-preamble.md`, `fnd-ledger.md`, `fnd-participants.md` by default — edit `config.json` to change)
- the coordination intention
- the scope artifact

…and is asked to respond with a single JSON ledger entry. The orchestrator
validates each entry against the schema in `fnd-ledger.md`, writes it to
`ledger/entries/NNN-{type}-{author}.json`, and creates one git commit per
entry.

## 4. Read the ledger

```bash
python orchestrator.py ledger
```

Or directly: `ls ledger/entries/`, `git log --oneline`.

For long sessions, use the **compressed view** instead:

```bash
python orchestrator.py ledger --summary
python orchestrator.py ledger --summary --scope scope/code/example_auth.py
```

The summary follows `fnd-ledger.md` → Read Protocol → Ledger Summary:

- `failure`, `repair`, `intention_shift`, and `boundary_change` entries are
  preserved **in full** (summary + detail). These are the highest-signal
  entries — they prevent repeated failures and define current operating
  conditions; lossy compression here would defeat the summary's purpose.
- `decision`, `attempt`, and `completion` entries on `--scope` (the
  "currently active" scope) are shown with their `summary` field only,
  detail omitted.
- Same types on other scopes are compressed to one line per entry.
- If `--scope` is not given, every decision/attempt/completion gets the
  one-line treatment.

If the resulting summary exceeds a soft threshold (~50 KB) the orchestrator
prints a **Balance warning** to the console, per `fnd-ledger.md`:

> The summary should be small enough to fit within the smallest context
> window of any active participant. If it is not, the coordination has
> grown beyond what its current participants can hold, and that itself
> is a signal (a Balance concern).

The summary is still returned in full when this fires — the warning is
signal for the human, not a refusal.

**Where the summary is wired into LLM context.** `run_repair` and
`run_synthesis` both inject the compressed summary as a `## Ledger Summary`
block at the top of the user message, with the active scope set to the
failure/synthesis scope. Invited participants receive the orienting context
the foundation says they should get on session resumption. `run_review` is
intentionally NOT wired — reviewers are scoped to a single artifact and
don't currently load history; injecting a summary there would be a separate
behavior change for reviewers worth discussing on its own.

## Convergence and the Conflict breaker (step 2)

When more than one agent reviews the same scope, the orchestrator treats
them as **converged** on it (per `fnd-participants.md` → Converge). At the
start of each review, a `decision` entry is written declaring:

- the converging participants
- the shared scope
- the conflict protocol (default: `escalate_to_repair`, set in `config.json`)

Each reviewer's `completion` entry must include a `verdict` field with one
of: `approve`, `approve_with_conditions`, `reject`, `escalate`, or
`no_judgment`. (This is a local extension to the `fnd-ledger.md` schema —
the framework defines the entry shape but does not standardize verdicts.
We add it so "incompatible findings" is mechanically detectable instead of
a vibe check.)

After all reviews land, the **Conflict circuit breaker** compares verdicts.
If two or more reviewers return different non-abstaining verdicts, the
breaker fires:

1. A `failure` entry is appended, tagged `["truth", "boundaries"]`, with
   `prior_entries` linking to the convergence decision and every conflicting
   completion.
2. The orchestrator surfaces the breach loudly and exits with status `3`.
3. The coordination is now paused on that scope until a `repair` entry is
   written (per `fnd-failure.md` → `fnd-repair.md`).

## Running the repair cycle

```bash
python orchestrator.py repair --failure-entry 004 --arbiter claude-sonnet
```

The arbiter can be any participant in `participants/declarations/`. The
orchestrator will:

1. Load `fnd-preamble.md`, `fnd-failure.md`, `fnd-repair.md`, `fnd-ledger.md`
   into the arbiter's context
2. Hand the arbiter the failure entry and the linked completions verbatim
3. Ask for a JSON `repair` entry covering Diagnosis → Resolution →
   Verification → Lessons, with `prior_entries` linked to the failure and
   the completions it considered
4. Validate, write, and git-commit the repair entry

If the arbiter is `human-lead` (which has no `litellm_model`), the command
prints the instructions and yields the cycle to you — you write the repair
entry by hand and commit it. This is the framework-honest fallback: humans
are participants, not failovers.

## Deployment via hermes (or any LLM gateway)

The orchestrator can route every LLM call through a single gateway endpoint
instead of calling provider APIs directly. This is the deployment pattern
for using **hermes** (or LiteLLM Proxy, OpenRouter, or any OpenAI-compatible
gateway): one config block reroutes the entire coordination through it.

Add a `hermes` block to `config.json`:

```json
{
  "hermes": {
    "api_base": "https://hermes.example.com/v1",
    "api_key_env": "HERMES_API_KEY"
  }
}
```

Then `export HERMES_API_KEY=...` and run normally. Every `litellm.completion`
call will hit the gateway with the gateway's auth, and the gateway routes
to whatever underlying provider/model the `litellm_model` field in the
declaration names. Multiple participants pointing at different models all
flow through the same gateway endpoint.

**Per-declaration override.** If a single participant needs to bypass the
gateway (e.g., using a model the gateway doesn't proxy), the declaration
can set its own `api_base` and `api_key_env`:

```json
{
  "identifier": "local-llama",
  "litellm_model": "openai/llama-3.3-70b",
  "api_base": "http://localhost:8080/v1",
  "api_key_env": "LOCAL_LLAMA_KEY",
  ...
}
```

**Resolution order** in `resolve_provider_routing`:
1. Per-declaration `api_base` (and its `api_key_env`) wins if set
2. Else config-level `hermes.api_base` (and `hermes.api_key_env`)
3. Else LiteLLM's default provider-prefix routing (`anthropic/...` uses
   `ANTHROPIC_API_KEY`, etc.)

**No code changes required to swap deployments.** Pointing the entire
coordination at hermes is one config edit and one env var. Pointing a
single participant somewhere else is two declaration fields. The script
itself is provider-agnostic.

## The orchestrator role is held by participants, not by the script

There is no routing system. There is an **orchestrator role** that a
participant takes for a specific scope, and a ledger that participants
can read and self-select from when no one holds it. The script
`orchestrator.py` is the **tool** the role-holder uses to do their work
— the script is not itself the orchestrator.

This is exactly what `fnd-field.md` says:

> A designated participant (human or agent) takes the orchestrator role
> for a defined scope. This role is declared in the ledger and carries
> a scope boundary — the orchestrator governs *this workflow*, not the
> entire coordination.
>
> The orchestrator is a participant, not a supervisor. It has a
> declaration. It has boundaries. It can be refused. It can be replaced.

**Taking and releasing the role.** The role is recorded as a `decision`
ledger entry with a `role_action` field set to `take_orchestrator` or
`release_orchestrator`. The author of these entries is the participant
taking the action.

```bash
# A participant declares they're taking the role for a scope
python orchestrator.py take-role --scope scope/code/example_auth.py --as human-lead

# Now `review` and `repair` on this scope work — they route on behalf of human-lead
python orchestrator.py review --scope scope/code/example_auth.py

# When done, the participant releases
python orchestrator.py release-role --scope scope/code/example_auth.py --as human-lead
```

**The state machine:**

| Action | Refused if |
|---|---|
| `take-role --as X` | Someone already holds the role for this scope, OR `X` is not in `participants/declarations/` |
| `release-role --as X` | `X` does not currently hold the role (you cannot release what you do not hold — that would be writing on behalf of another participant) |
| `review` / `repair` | No one holds the orchestrator role for the scope |
| `synthesize` | No one holds the role to *initiate* the transition (the role is auto-released as part of the transition itself, since Emergent Mode is roleless) |

**What the script does without a role-holder:** Infrastructure Mode work
only — schema validation, signal envelope dispatch, ledger reads, breaker
monitoring. These are always available because they're automated services
that don't make orchestration decisions:

```bash
python orchestrator.py inbox process    # always works
python orchestrator.py inbox list       # always works
python orchestrator.py ledger           # always works
python orchestrator.py take-role ...    # always works (it's how you create a role-holder)
```

**What the script will NOT do without a role-holder:** anything that
requires routing decisions or task assignment:

```bash
python orchestrator.py review --scope X    # refuses with status 2
python orchestrator.py repair ...          # refuses with status 2
python orchestrator.py synthesize --scope X # refuses with status 2
```

**Why entries are no longer authored by `"orchestrator"`.** The literal
string `"orchestrator"` was a fictional identity — no participant in
`participants/declarations/` had it, but the script was using it as the
author of convergence decisions, mode transitions, signal-derived
entries, and so on. That was a Truth violation: entries written under
an identity nobody held. After this change:

- **Routing entries** (convergence decisions, mode transitions,
  intention shifts, conflict failures) are authored by the **role-holder**
  who routed the work.
- **Signal-derived entries** (recommendations becoming `decision` entries,
  boundary changes, error reports) are authored by the **signal source**
  — the participant who sent the envelope. The signal IS their authorization
  to make the state change; the script is just the mechanism.
- **In Emergent Mode** (post-synthesis-transition), there's no role-holder.
  Ledger entries written during emergent self-selection are authored by
  the self-selecting participant.

**The synthesize flow auto-releases the role.** Opening synthesis transitions
the scope from Orchestrated to Emergent. Emergent Mode has no role-holder,
so the role-holder's last act is releasing the role. After `synthesize`
completes, you'll see three orchestrator-role-related entries on the scope:
the original `take_orchestrator`, the mode transition decision, and the
auto-`release_orchestrator`. To run another `review` on the same scope after
synthesis, you'd need to take the role again.

## Self-selection in Emergent Mode

When no participant holds the orchestrator role for a scope, the scope is
in Infrastructure or Emergent Mode. Per `fnd-field.md → Emergent Mode`:

> No one holds a special role. Participants read the ledger, identify
> where they can contribute, propose their involvement via signal, and
> begin work when acknowledged.

The `self-select` command is how a participant declares they're picking
up scope:

```bash
python orchestrator.py self-select --scope scope/code/foo.py --as claude-sonnet --reason "noticed in Emergent Mode"
```

This writes an `attempt` ledger entry (per `fnd-participants.md → Accept`:
"Acceptance is recorded as an `attempt` entry") authored by the
self-selecting participant, tagged `[choice, intention]`. Subsequent work
the participant produces on this scope should link back via `prior_entries`
to the attempt entry — that's how the Recursion chain stays traceable in
Emergent Mode.

**Refused if the scope is in Orchestrated Mode.** If a role-holder exists,
self-selection is wrong: routing happens through the role-holder, not
around them. The error message tells the would-be self-selector to ask
the role-holder for routing or to wait for them to release.

## Role transfer with state snapshot

Per `fnd-participants.md → Transfer`:

> 1. The outgoing participant writes a state snapshot to the ledger —
>    what was done, what remains, what was learned.
> 2. The incoming participant acknowledges receipt before assuming
>    ownership.
> 3. Both the release and the acknowledgment are recorded as ledger
>    entries.

Transfer is a two-command flow using existing `release-role` and
`take-role` with new options:

```bash
# Step 1: outgoing participant releases with a snapshot, naming the recipient
python orchestrator.py release-role --scope X --as alice \
  --reason "transferring to bob — different focus needed" \
  --snapshot @path/to/snapshot.md \
  --to bob

# Step 2: recipient takes the role, acknowledging the release
python orchestrator.py take-role --scope X --as bob --acknowledging <release-entry-id>
```

The `--snapshot` value can be either a literal string or `@path/to/file.md`
to read from disk. It becomes a `## State Snapshot` section in the release
entry's detail field. The `--to` flag records the intended recipient in the
release entry's summary and detail.

The `--acknowledging` flag on `take-role` validates that the named entry
is a `release_orchestrator` entry on the same scope, then writes a take
entry with the release entry id in `prior_entries` and a `## Acknowledging
Transfer` section in the detail. The lineage chain is now traversable in
both directions.

**Between release and take, the role is unheld.** That's framework-honest:
the outgoing participant has explicitly stepped down before the new holder
has stepped up. Routing operations on the scope are refused during this
window. If the recipient never acknowledges, the release stands and the
scope stays in transition until someone else takes the role (acknowledging
the release or not).

**Why two commands instead of one `transfer-role`.** The framework's
"transfer requires acknowledgment from the receiving participant" maps
naturally to two commands run by two participants. A single `transfer-role`
command would either have to write the take on behalf of the recipient
(which is exactly the on-behalf-of-another-participant pattern the
framework forbids) or be sugar for the two-command sequence anyway.

## The Repetition circuit breaker

Per `fnd-failure.md`:

> **Repetition** | 3+ `attempt` entries on the same scope without an
> intervening `completion`, `failure`, or `repair` entry.

In our orchestrator this is implemented as: **3+ unrepaired failure
entries on the same scope** triggers the breaker. A failure is "repaired"
when a `repair` entry links back to it via `prior_entries`. Three failures
in a row without resolution means the scope keeps breaking and the
coordination has no memory of why — exactly the Recursion failure the
breaker exists to catch.

The check runs at the start of every `review` call, before any LLM work.
If the breaker fires:

- **With a role-holder**: a `failure` entry is written (authored by the
  role-holder), tagged `[recursion, balance]`, with `prior_entries` listing
  every unrepaired failure. The review refuses with status 3.
- **Without a role-holder**: the breaker is surfaced to the console with
  the same diagnosis, but no entry is written (writing one would require
  attribution and there's no participant to attribute it to). The review
  refuses with status 3.

Either way, the path forward is to run `repair` on each unresolved
failure, OR to release the role and let the field move to Emergent Mode
with a different framing, OR to write an `intention_shift` entry
redefining the scope.

## Handoff envelopes — orchestrator → agent calls have a trace

Every LLM call now writes a `handoff` signal envelope to `signal/archive/`
before the call goes out. The envelope is authored by the role-holder
(or the self-selecting participant in Emergent Mode), addressed to the
agent being called, with `payload.task_type` set to one of `review`,
`repair`, or `synthesis_invitation`.

This completes the bidirectional trace: agent → orchestrator signals were
already going through the inbox; orchestrator → agent calls now leave a
matching outbound envelope. Reading `git log` on `signal/archive/` shows
the full sequence of cross-participant signals for any session.

**Note:** the handoff envelope is written before the LLM call but the
*actual call content* (system + user messages) is not stored in the
envelope. The envelope captures the routing fact ("X handed off Y to Z
on lineage [...]") but not the prompt text. If the prompt needs to be
auditable, that would be a future addition (maybe envelope.payload.prompt).

## Bidirectional enmeshment: signal envelopes

Agents can include zero or more **signal envelopes** alongside their
requested ledger entry. The orchestrator extracts them, dispatches each via
a per-type handler, and archives them. This is the load-bearing piece for
true bidirectional enmeshment — agents can now call the orchestrator
unsolicited (recommend a participant, declare reduced capacity, flag a
foundation concern) without being prompted.

A signal envelope (per `fnd-preamble.md`) looks like:

```json
{
  "signal_id": "AUTO",
  "origin": "claude-sonnet",
  "destination": "orchestrator",
  "timestamp": "AUTO",
  "type": "query",
  "payload": {
    "recommendation": "Add gemini-pro for long-context plan review",
    "capability_gap": "project plan feasibility analysis",
    "rationale": "..."
  },
  "context_summary": "Recommending a participant based on observed gap",
  "confidence": 0.7,
  "lineage": []
}
```

**Per-type handlers:**

| Signal type | What the handler does |
|---|---|
| `query` (with `recommendation`/`capability_gap` in payload) | Writes a `decision` ledger entry tagged `[choice, boundaries]` per `fnd-participants.md → Discovery`. The recommended agent (if accepted) must provide its own declaration — no participant declares on behalf of another. |
| `query` (generic) | Surfaces to console for human response. No ledger entry. |
| `boundary_change` | Writes a `boundary_change` ledger entry recording the declared change. **Does not modify the static declaration file** in `participants/declarations/` — permanent declaration changes are human-curated. |
| `error` (with `foundations` cited) | Writes a `failure` ledger entry tagged with the cited foundations and surfaces loudly. The coordination should consider entering the repair cycle. |
| `error` (no foundation cited) | Surfaces to console only. |
| `handoff`, `state_update`, `acknowledgment` | Default handler — surfaced to console and archived. Specialized handlers for these types are future work (peer-to-peer routing, convergence resolution, lineage tracing). |

**Inbox flow:**

1. Signal arrives (either from an agent's response during review/repair/synthesis,
   or from a hand-written JSON file dropped into `signal/inbox/`)
2. Orchestrator writes the signal to `signal/inbox/{signal_id}.json`
3. The per-type handler runs (may write a ledger entry)
4. Signal moves to `signal/archive/{signal_id}.json` and is git-committed
5. `signal/inbox/` is `.gitignore`d (transient); `signal/archive/` is tracked
   so the lineage chain survives across sessions per `fnd-signal.md`

**Offline testing — no API keys required:**

You can exercise the entire signal pipeline without LiteLLM by hand-writing
an envelope and running `inbox process`:

```bash
mkdir -p signal/inbox
cat > signal/inbox/sig-test.json <<'EOF'
{
  "signal_id": "sig-test",
  "origin": "claude-sonnet",
  "destination": "orchestrator",
  "timestamp": "2026-04-09T15:30:00Z",
  "type": "query",
  "payload": {
    "recommendation": "Add gemini-pro for long-context plan review",
    "capability_gap": "project plan feasibility analysis",
    "rationale": "longer context window for plan-level artifacts"
  },
  "context_summary": "Recommending a participant for next session",
  "confidence": 0.7,
  "lineage": []
}
EOF

python orchestrator.py inbox list      # shows the pending signal
python orchestrator.py inbox process   # dispatches via handle_query
python orchestrator.py inbox list      # shows it in archive
python orchestrator.py ledger          # shows the new decision entry
```

The test above is exactly how this feature was validated.

**What this does NOT yet build:**

- Agent → orchestrator signals are now possible, but **orchestrator → agent
  signals are still implicit** in the system prompt. Both directions should
  eventually flow through the envelope schema. Right now the inbox is
  one-and-a-half directional.
- Specialized handlers for `handoff`, `state_update`, and `acknowledgment`.
  The default handler records and surfaces them but doesn't act.
- Lineage validation — signal envelopes can claim arbitrary `lineage` arrays
  and the orchestrator doesn't currently verify the referenced signals exist.

## Enmeshment: validate-and-retry, no force

The orchestrator and active agents are peers — neither has priority over the
other. In code this manifests as the **validate-and-retry-with-error**
pattern in `request_entry_with_retry`:

- Every agent call is a multi-turn conversation, not a one-shot
- If the agent's response fails validation (parse error, type mismatch,
  missing required lineage, author mismatch), the orchestrator does NOT
  silently rewrite the entry. It appends the validation error as the next
  user message and gives the agent another turn — up to ~2 retries
- A response of `type: failure` is **always** accepted as a refusal. The
  orchestrator does not coerce a refusal into the requested type. Refusal
  is signal, not malfunction.
- If the agent persistently cannot produce a valid entry, the orchestrator
  writes a `failure` entry recording *the participant's inability to
  converge under current task framing*. This is signal too — likely a
  Signal/Intention/Recursion concern that the next session can diagnose.

The orchestrator never:
- Sets `raw["type"] = "..."` to override what the agent returned
- Inserts ids into `prior_entries` to fix the agent's lineage claim
- Appends to `foundation_tag` to fix the agent's tagging claim
- Continues silently after a failed validation

Every one of those moves was in earlier versions of this orchestrator and
all of them have been removed. They were hierarchy-by-default — supervisor
moves dressed up as cleanup.

**Note on bidirectional enmeshment:** the validate-and-retry pattern is
half of the enmeshment story (multi-turn agent ↔ orchestrator within a
single task). The other half is the **signal inbox** — see the section
above. Together they let agents and the orchestrator exchange signals in
both directions, neither overriding the other.

## Synthesis as Emergent Mode transition (step 3)

Synthesis is **not** a single-arbiter operation. It is a field mode
transition: the orchestrator moves the scope from Orchestrated to Emergent
Mode (per `fnd-field.md`), records the synthesis question as an
`intention_shift` entry (per the same module: "A transition to Emergent
Mode should be accompanied by a clear statement of the question being
explored, recorded in the ledger as an `intention_shift` entry"), and
surfaces the question to **every** active participant. Each participant
SELF-SELECTS — they may propose a synthesis decision OR refuse with reason.
The aggregate of those proposals IS the synthesis.

```bash
python orchestrator.py synthesize --scope scope/code/example_auth.py
```

There is no `--synthesizer` flag. There is no synthesizer role. The
orchestrator does not author a "unified" decision over the proposals; it
prints a mechanical aggregation panel for the human, who reads the proposal
entries directly in the ledger.

**The full sequence on a single scope:**

1. `synthesize` checks the safety guardrail (refuses if any `failure` entry
   on the scope has no `repair` linking back — synthesizing over an open
   breaker would route around it, which `fnd-field.md` forbids)
2. Orchestrator writes a `decision` entry recording the mode transition
   (orchestrated → emergent), tagged `["choice", "intention"]`
3. Orchestrator writes an `intention_shift` entry recording the open
   question, tagged `["intention"]`
4. For each active participant with a `litellm_model` (including the
   original reviewers — their amended view is signal), the orchestrator
   surfaces:
   - the convergence decision
   - every completion entry linked to it
   - any repair entries that resolved earlier conflicts
   - the original scope artifact
   - `fnd-preamble.md`, `fnd-field.md`, `fnd-ledger.md`, `fnd-signal.md`
5. Each participant returns either:
   - A `decision` entry proposing their synthesis position (with verdict,
     summary, structured detail, and prior_entries the participant chose
     to claim), OR
   - A `failure` entry refusing with reason (e.g., "I already reviewed this
     scope and my synthesis would inherit my prior framing")
6. The orchestrator runs `print_synthesis_aggregation`: a Rich table of
   every invitee's outcome, plus a verdict-count panel showing whether the
   proposals are convergent, divergent, or absent

**Convergent proposals** (all proposals share the same verdict) are surfaced
as strong signal. **Divergent proposals** are also legitimate output —
*the aggregate IS the signal*. The human reads each proposal entry in the
ledger and forms their own position. The orchestrator does not pick.

**Bias safeguard.** Original reviewers participate in synthesis just like
anyone else. The bet is on participant honesty: an agent that already
reviewed the scope is asked, in the system prompt, to consider whether
their synthesis would inherit their prior framing — and to refuse with
that reason if so. The framework's bet is on participant honesty rather
than on architecture preventing bias (`fnd-preamble.md` Truth: "Suppressing
uncertainty is deception" applies symmetrically to suppressing acknowledged
bias).

**What if no one self-selects?** That is also signal. The orchestrator
records the failures/refusals and the human decides whether to escalate,
expand the participant roster, or close the scope unsynthesized.

**Synthesis is NOT repair.** Repair handles disagreement (Conflict breaker
fired). Synthesis handles agreement-with-fragmentation. They produce
different entry shapes and use different field modes (repair stays in
Orchestrated, synthesis transitions to Emergent).

## Capability-based routing

Reviews are no longer broadcast to every active agent. The orchestrator
now routes based on each participant's `preferred_tasks` declaration field.

```bash
python orchestrator.py review --scope scope/code/example_auth.py
python orchestrator.py review --scope scope/code/example_auth.py --task-type code_review
```

**How routing works:**

1. If `--task-type` is given, use it. Otherwise, infer from the file
   extension (`.py` → `code_review`, `.md` → `writing_review`, etc.).
2. Filter active agents to those whose `preferred_tasks` includes the
   task type. Sort by `capability_envelope` score (descending).
3. If no agents match (or no task type could be determined), fall back
   to the original broadcast behavior — all active agents review.

The review panel shows whether capability routing was applied or whether
it fell back to broadcast.

## Resource and Timeout circuit breakers

Two new circuit breakers complete the set described in `fnd-failure.md`:

**Resource breaker** — fires when one participant's token usage in the
current session exceeds N× the per-participant average (N =
`config.circuit_breakers.resource_multiplier`, default 2.0). Also fires
if a participant exceeds their declared `resource_ceiling.max_tokens_per_session`.
Token tracking is session-scoped (resets on script restart). The check
runs after `run_review` completes, alongside the Conflict breaker.

**Timeout breaker** — fires when a signal in `signal/archive/` has gone
unacknowledged past the destination participant's declared
`context_constraints.latency_tolerance_seconds`. The check runs at the
end of `python orchestrator.py inbox process`. An unacknowledged signal
past tolerance may indicate ungraceful departure.

Both breakers write `failure` entries and surface loudly, consistent with
the existing Conflict and Repetition breakers.

## Verification rerun

After a repair entry is written, the original failing reviewers can be
automatically re-run under the resolved conditions:

```bash
python orchestrator.py repair --failure-entry 004 --arbiter claude-sonnet --verify
```

The `--verify` flag triggers a limited re-review: each original reviewer
receives the repair entry and the scope artifact, and produces a new
`completion` entry. If any reviewer's verdict is `reject` or `escalate`,
the verification signals that the repair may not have held.

Without `--verify`, the repair entry stands as-is (the default). Per
`fnd-repair.md`: "Verification must either include a limited rerun of
the failed work under the resolved conditions, or explicitly record why
rerun is impossible or unsafe."

## Mode return after Emergent synthesis

After synthesis completes (all invitees have responded), the orchestrator
now writes a closing `decision` entry transitioning the scope from
Emergent back to Infrastructure mode. This entry:

- Is authored by the original role-holder who initiated synthesis (their
  identity persists even though the role was released during the transition
  to Emergent mode)
- Records whether proposals were convergent or divergent, the verdict
  distribution, and the proposal/refusal/failure counts
- Links back to the original mode-transition entry via `prior_entries`

After mode return, the scope is at rest. A participant must `take-role`
again to begin new orchestrated work on the same scope.

## Signal lineage validation

Signal envelopes can no longer claim arbitrary `lineage` arrays without
scrutiny. When a signal is processed (via `process_signal` or
`inbox process`), the orchestrator validates that every signal_id in the
envelope's `lineage` array exists in `signal/archive/`.

Missing references produce a yellow warning but do **not** block
processing. A gap in the lineage chain is degraded signal, not a reason
to drop a legitimate message.

## Handoff envelopes capture prompt text

Outgoing handoff envelopes can now include the full system+user messages
sent to agents as `payload.prompt`. This is **opt-in** via config:

```json
{
  "capture_prompt_in_handoff": true
}
```

When enabled, every `write_outgoing_handoff` call stores the complete
message list in the archive. This makes the handoff fully auditable:
reading `git log` on `signal/archive/` shows not just *who handed off
what to whom*, but the exact prompt text the agent received.

Default is `false` because it substantially increases archive file sizes.

## Specialized signal handlers for state_update and acknowledgment

`state_update` and `acknowledgment` signals now have real handlers instead
of falling through to the default:

**state_update handler:**
- If `payload.proposed_entry` contains a full ledger entry dict, validates
  and writes it directly (the signal IS the participant's proposal per
  fnd-ledger.md Write Protocol).
- If `payload.scope` + `payload.state` are present, synthesizes an
  `attempt` entry from the payload.
- Otherwise, surfaces to console (backward compatible fallback).

**acknowledgment handler:**
- `payload.response = "accept"` or `"accept-with-conditions"` → writes
  an `attempt` entry (per fnd-participants.md, acceptance is recorded as
  an attempt entry).
- `payload.response = "refuse-with-reason"` → writes a `decision` entry
  recording the refusal so other participants can see the scope is available.
- Validates that acknowledged signal ids in `lineage` exist in archive.
- Unstructured acknowledgments (no `response` field) are surfaced to
  console only.

## Iteration history

1. ✅ ~~Conflict breaker → repair cycle~~ (step 2)
2. ✅ ~~Synthesis as collaborative emergent transition~~ (step 3)
3. ✅ ~~Signal envelope inbox/archive~~ (step 4)
4. ✅ ~~Orchestrator role as a thing held by participants~~ (step 5)
5. ✅ ~~Hermes deployment, role transfer, self-select, Repetition breaker, handoff envelopes~~ (step 6)
6. ✅ ~~Ledger summary generation~~ (step 7) — `python orchestrator.py ledger --summary`
7. ✅ ~~Capability-based routing~~ (step 8) — `review --task-type`, `route_participants()`, `infer_task_type()`
8. ✅ ~~Resource + Timeout circuit breakers~~ (step 8) — session token tracking, resource breaker, timeout breaker via `inbox process`
9. ✅ ~~Specialized handlers for state_update and acknowledgment signals~~ (step 8) — `handle_state_update()`, `handle_acknowledgment()`
10. ✅ ~~Mode return after Emergent synthesis~~ (step 8) — closing decision: emergent → infrastructure
11. ✅ ~~Signal lineage validation~~ (step 8) — `validate_signal_lineage()`, warn on missing refs
12. ✅ ~~Handoff prompt capture~~ (step 8) — `payload.prompt` opt-in via `capture_prompt_in_handoff` config
13. ✅ ~~Verification rerun~~ (step 8) — `repair --verify`, `run_verification_rerun()`
