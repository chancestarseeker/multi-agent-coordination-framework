# Multi-Agent Coordination for Hermes

An implementation layer that bridges the Coordination Foundations architecture (`fnd-*.md`) to [Hermes Agent](https://hermes-agent.nousresearch.com/) by Nous Research.

## The Problem

Hermes's native multi-model support — fallback providers, provider routing, subagent delegation — is hierarchical and failure-driven. The Coordination Foundations require peer participants with consent, shared memory, and self-organization. This project bridges the gap.

## Architecture

```
┌─────────────────────────────────────────────────┐
│  Human / Messaging Gateway (Telegram, CLI, etc) │
└──────────────────────┬──────────────────────────┘
                       │
              ┌────────▼────────┐
              │  Hermes Agent   │  ← Orchestrated Mode
              │  + Coord Skill  │    (reads SKILL.md)
              └────────┬────────┘
                       │ HTTP
              ┌────────▼────────┐
              │  Coordination   │  ← Infrastructure Mode
              │     Daemon      │    (schema, breakers, dispatch)
              │   :8420         │
              └──┬─────┬────┬──┘
                 │     │    │
          ┌──────▼┐ ┌──▼──┐ ▼──────────┐
          │Ledger │ │Reg. │ │ Dispatch  │
          │.jsonl │ │.yaml│ │ → Hermes  │
          └───────┘ └─────┘ └───────────┘
```

**Coordination Daemon** — Lightweight Python service (not an LLM). Owns the append-only ledger, validates entries against the schema, detects conflicts, monitors circuit breaker thresholds, and dispatches Hermes sessions when events warrant them. This is Infrastructure Mode enacted as an actual service.

**Hermes + Coordination Skill** — The Hermes agent loads `SKILL.md` (in this directory) which teaches it to operate as an orchestrator within the foundations. It reads the ledger on session start, writes entries frequently, delegates with signal envelopes, and respects consent (participants may refuse).

**Ledger** — Append-only JSONL file. Every entry carries: what changed, why, what it means for what comes next, which foundations are relevant, and the author's confidence. No participant owns it. The daemon validates and appends.

**Participant Registry** — YAML file declaring available models with their capability envelopes, cost models, context constraints, and boundaries. The daemon's `/select` endpoint routes by capability and cost — replacing Hermes's static fallback hierarchy with foundation-aware routing.

## Quick Start

This `hermes/` directory lives inside the parent `agent-coordination/`
repo, which also contains the canonical `foundations/` directory one level
up. The daemon and the Hermes skill both reference `../foundations/` —
no copy step is needed.

```bash
# 1. Install daemon dependencies (from this directory)
pip install aiohttp pyyaml jsonschema

# 2. Start the daemon
python daemon.py --config coordination.yaml

# 3. (Optional) Install the Hermes skill into your Hermes config
mkdir -p ~/.hermes/skills/coordination
cp SKILL.md ~/.hermes/skills/coordination/
# Foundations already exist at ../foundations/ relative to this directory.
# If your Hermes installation needs a copy in its skill directory, link or copy:
cp ../foundations/fnd-*.md ~/.hermes/skills/coordination/

# 4. Start Hermes — it will read the skill and connect to the daemon
hermes
```

## File Structure (within agent-coordination/)

```
agent-coordination/
├── foundations/                       # ← canonical fnd-*.md files (shared)
├── cli/                               # single-process Python implementation
└── hermes/                            # ← you are here
    ├── README.md                      # this file
    ├── SKILL.md                       # Hermes Agent skill (loaded by Hermes)
    ├── CLAUDE.md                      # quick reference card
    ├── coordination.yaml              # daemon + breaker config
    ├── daemon.py                      # the coordination daemon
    ├── ledger-entry.schema.json       # ledger entry JSON schema
    ├── participant-declaration.schema.json
    ├── participants.yaml              # model declarations
    └── ledger.jsonl                   # created on first append (gitignored)
```

## Daemon API

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/append` | POST | Propose a ledger entry. Returns validation errors or conflict if detected. Fires circuit breakers after successful append. |
| `/query` | GET | Scoped read. Params: `scope`, `type`, `author`, `last_n` |
| `/summary` | GET | Ledger summary for session bootstrapping. Param: `max_tokens` |
| `/registry` | GET | Current participant declarations |
| `/select` | GET | Route by capability and cost. Params: `capability`, `max_cost` |
| `/health` | GET | Daemon health, entry count, active scopes, resource totals |

## How Consent Works

Hermes's native `delegate_task` doesn't support consent — subagents just execute. The coordination skill bridges this by:

1. Injecting a consent preamble into every delegation's `context` field.
2. Instructing the subagent that it may respond with `REFUSE: [reason]`.
3. Parsing the subagent's summary for refusal signals.
4. On refusal: writing a `decision` entry to the ledger and routing to an alternative via `/select`.

This is not mechanical enforcement — it's self-declared (the subagent chooses to follow the consent instruction). The foundations acknowledge this distinction in the `enforcement` field of participant declarations.

## How Self-Organization Works (Emergent Mode)

For Emergent Mode without central routing:

1. Multiple Hermes instances (or cron-triggered sessions) read the same ledger.
2. Each evaluates unowned scope and self-selects based on its own assessment.
3. The daemon's conflict detection prevents two participants from claiming the same scope.
4. No orchestrator needed — the ledger is the coordination.

The daemon dispatches sessions event-driven (on ledger changes), not clock-driven (polling). This avoids burning Balance on empty ticks.

## Circuit Breakers

The daemon monitors and fires automatically:

| Breaker | Threshold | Foundation |
|---------|-----------|------------|
| Repetition | 3+ attempts same scope without resolution | Recursion |
| Resource | 2× per-participant average cost or tokens | Balance |
| Confidence | < 0.3 on task with no fallback | Truth |
| Conflict | Two incompatible proposals on same scope | Truth |
| Timeout | Signal unacknowledged past declared latency | Boundaries |

When a breaker fires, the daemon writes a `failure` entry and dispatches a repair session.

## Known Limitations

- **Subagent depth cap**: Hermes limits delegation to depth 2, max 3 parallel. True peer-to-peer convergence within a single session isn't possible.
- **No inter-subagent communication**: Subagents can't signal each other. All coordination flows through the parent or the ledger.
- **Consent is self-declared**: Subagents aren't mechanically prevented from ignoring the refuse option.
- **Emergent Mode requires multiple instances**: A single Hermes session is always orchestrated. Self-organization requires multiple sessions reading the shared ledger.

## Future: Psyche Integration

Nous Research's Psyche network coordinates distributed GPU training on Solana. Its architecture — on-chain coordinator, witnessing, fault-tolerant participation — maps conceptually to this coordination's needs. If Psyche extends to inference coordination (per their roadmap), it could replace the daemon with a decentralized, blockchain-backed coordination layer. The ledger would become on-chain state; participant declarations would be smart contract entries; circuit breakers would be on-chain validators.
