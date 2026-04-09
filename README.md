# Agent Coordination

A multi-AI-agent coordination architecture grounded in the **Foundations
of Multi-AI-Agent Coordination v0.4** — a six-foundation framework
(Choice, Boundaries, Truth, Balance, Recursion, Intention) that treats
agents as peer participants rather than subordinates in a hierarchy.

This repository contains the framework specification and **two reference
implementations**: a single-process Python CLI for local development and
a daemon + skill targeting Hermes Agent by Nous Research for production
deployment. The two implementations are layered, not duplicates — they
share one canonical `foundations/` directory and represent different
deployment shapes of the same architecture.

```
agent-coordination/
├── README.md                          ← this file
├── SKILL.md                            ← Claude Code skill entry point
├── coordination-tech-stack.md          ← original blueprint
│
├── foundations/                        ← canonical framework specification
│   ├── fnd-preamble.md                 the six foundations + signal envelope + breakers + Module Index
│   ├── fnd-participants.md             participant declaration schema + scope actions
│   ├── fnd-field.md                    Infrastructure / Orchestrated / Emergent modes
│   ├── fnd-ledger.md                   ledger entry schema + read/write protocol
│   ├── fnd-signal.md                   how signal is strengthened and how it is lost
│   ├── fnd-failure.md                  how each foundation breaks + circuit breakers
│   └── fnd-repair.md                   the repair cycle and its principles
│
├── cli/                                ← single-process Python CLI
│   ├── README.md                       full setup + command reference
│   ├── orchestrator.py                 reference implementation (~3100 lines)
│   ├── config.json, requirements.txt, .gitignore
│   ├── participants/declarations/      claude-sonnet, gpt-4o, human-lead
│   └── scope/code/example_auth.py      example artifact for review
│
└── hermes/                             ← daemon + Hermes Agent skill
    ├── README.md                       deployment overview + HTTP API reference
    ├── SKILL.md                        loaded by Hermes Agent (not Claude Code)
    ├── CLAUDE.md                       quick reference card
    ├── daemon.py                       HTTP service on :8420 (~660 lines)
    ├── coordination.yaml               daemon config
    ├── participants.yaml               4 participants with hermes routing fields
    └── *.schema.json                   JSON schemas for ledger entries and declarations
```

## The framework, in two paragraphs

Coordination is not a routing system. It is six commitments — Choice,
Boundaries, Truth, Balance, Recursion, Intention — and a shared
append-only ledger that survives the absence of individual memory.
Participants (AI agents, humans, or anything that can send and receive
signal) enter via a declaration that names their capabilities, boundaries,
costs, and known limitations. They propose state changes to the ledger
rather than modifying it directly. Conflicts are surfaced, never
last-write-wins. Refusal is signal, not malfunction.

The architecture has three modes: **Infrastructure** (automated services
— validation, schema checking, breaker monitoring), **Orchestrated** (a
participant takes the orchestrator role for a specific scope and routes
work to others), and **Emergent** (no orchestrator, participants
self-select from the ledger). Mode transitions are explicit ledger
entries. Five circuit breakers fire when foundations are under strain
(Timeout, Conflict, Resource, Confidence, Repetition); when one fires,
the coordination enters a repair cycle rather than continuing with
degraded foundations.

The full specification is in `foundations/fnd-*.md` (seven files, about
700 lines total). Read `fnd-preamble.md` first.

## Quick start: CLI implementation

Single-process Python tool. Good for local testing, small coordinations,
and exploring the framework hands-on.

```bash
cd cli
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# API keys for the default participants (or configure a hermes gateway —
# see config.json's "hermes" block to route everything through one endpoint)
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...

# Take the orchestrator role for a scope
python orchestrator.py take-role --scope scope/code/example_auth.py --as human-lead

# Run a multi-agent review (claude-sonnet + gpt-4o review the same artifact)
python orchestrator.py review --scope scope/code/example_auth.py

# Read the ledger
python orchestrator.py ledger

# If reviewers disagreed (Conflict breaker fired), run repair
python orchestrator.py repair --failure-entry <id> --arbiter claude-sonnet

# Open synthesis (Emergent Mode transition with self-selection)
python orchestrator.py synthesize --scope scope/code/example_auth.py

# Release the role when done
python orchestrator.py release-role --scope scope/code/example_auth.py --as human-lead
```

The CLI also supports role transfer with state snapshot, self-selection
in Emergent Mode, the signal envelope inbox for unsolicited agent → orchestrator
calls, and routing every LLM call through any OpenAI-compatible gateway
(hermes, LiteLLM Proxy, OpenRouter, etc.) via a single config block.
Full reference: [`cli/README.md`](cli/README.md).

## Quick start: hermes deployment

Daemon + Hermes Agent skill. Production deployment form. The daemon is
not an LLM — it's a pure HTTP service that owns the ledger, validates
entries, runs breakers, and dispatches Hermes sessions in response to
ledger events.

```bash
cd hermes
pip install aiohttp pyyaml jsonschema

# Start the daemon (uses ../foundations/ from the parent agent-coordination/ root)
python daemon.py --config coordination.yaml

# In another shell — verify the daemon
curl http://localhost:8420/health
curl http://localhost:8420/registry
curl "http://localhost:8420/select?capability=code_generation&max_cost=0.01"

# Install the Hermes skill so Hermes Agent loads it on startup
mkdir -p ~/.hermes/skills/coordination
cp SKILL.md ~/.hermes/skills/coordination/
cp ../foundations/fnd-*.md ~/.hermes/skills/coordination/

# Start Hermes
hermes
```

Hermes Agent then operates as the orchestrator role-holder, reading the
ledger via `/summary` at session start, writing entries via `/append`,
routing via `/select`, and injecting consent preambles into delegations.
The daemon enforces schema, fires breakers, and triggers repair sessions
automatically. Full architecture and API reference:
[`hermes/README.md`](hermes/README.md).

## How the two implementations relate

| | **cli** (single-process) | **hermes** (daemon + skill) |
|---|---|---|
| **Infrastructure Mode** | inline in `orchestrator.py` | physically separated as `daemon.py` |
| **Orchestrator role** | held by a participant via `take-role` CLI | held by Hermes Agent via the skill |
| **Ledger storage** | one JSON file per entry (`cli/ledger/entries/`) | append-only JSONL (`hermes/ledger.jsonl`) |
| **Entry IDs** | sequential `001`, `002`, ... | distributed `{author}-{epoch_ms}-{random}` |
| **Participants** | JSON files in `cli/participants/declarations/` | YAML in `hermes/participants.yaml` |
| **Routing** | direct `litellm.completion` with optional gateway | daemon `/select` HTTP endpoint |
| **Signal envelopes** | files in `cli/signal/inbox/` and `cli/signal/archive/` | embedded as `signal_envelope` field on entries |
| **Conflict detection** | semantic (verdict mismatch) | temporal (60-second window, scope match, different authors) |
| **Use case** | local development, demos, small coordinations | production deployment, multi-instance coordinations |

The **canonical convergence path** (deferred work): make
`cli/orchestrator.py` daemon-aware (HTTP client mode pointing at
`hermes/daemon.py`), then port the CLI's structural additions
(`role_action`, `verdict`, `task_response`, self-select, transfer flow,
validate-and-retry helper) into the daemon's schema and handlers. Both
implementations end up sharing one source of truth: the daemon's JSONL
ledger and HTTP API.

## What is intentionally not built yet

Both implementations honor the framework's foundations but neither is
feature-complete. The current deferred list:

| Item | Where | Notes |
|---|---|---|
| Daemon-aware mode for the CLI | `cli/orchestrator.py` | Track A in convergence — would let CLI commands hit the daemon's HTTP API instead of touching local files |
| Port CLI additions to the daemon | `hermes/daemon.py` | Track B — `role_action`, `verdict`, `task_response`, self-select, transfer flow, validate-and-retry helper |
| Ledger summary generation in CLI | `cli/orchestrator.py` | Specified in `fnd-ledger.md`; daemon's `Ledger.summary()` already does this |
| Resource + Timeout breakers in CLI | `cli/orchestrator.py` | Daemon has Resource; Timeout is unimplemented in both |
| Capability-based routing in CLI | `cli/orchestrator.py` | Daemon's `/select` already does this |
| Cron safety net for slow-degrading failures | `hermes/` | Per `fnd-failure.md`'s Monitoring practice paragraph — fallback for when event-driven dispatch misses |
| Mode return after Emergent synthesis | `cli/orchestrator.py` | Synthesis transitions to Emergent but doesn't write a closing return-to-orchestrated entry |
| Specialized signal handlers | both | `handoff`/`state_update`/`acknowledgment` types use a default handler |
| Wrap orchestrator → agent calls in explicit envelopes (with prompt text) | `cli/orchestrator.py` | Step 6 added handoff envelope writing; the envelope captures routing facts but not the full prompt |

## How to keep `foundations/` canonical

The seven framework files in `foundations/` are the **single source of
truth**. Both `cli/orchestrator.py` and `hermes/daemon.py` reference
`../foundations/` from their respective subdirectories. If you edit any
foundation file, both implementations pick up the change immediately —
no copy step.

If you're publishing the `cli/` or `hermes/` directories somewhere they
can't reach the parent `foundations/`, you'll need to either copy the
files in or modify the reference path in `cli/orchestrator.py`'s
`FND_DIR` and `hermes/CLAUDE.md`.

## Acknowledgments

The framework specification (the seven `fnd-*.md` files) is the result of
several conversations developing a consent-based, ledger-grounded
alternative to hierarchical orchestration. The two implementations were
built incrementally — each step honoring more of the framework than the
last and surfacing where the framework's commitments were being violated
by earlier shortcuts.

The framework foundations are framework-agnostic and freely usable. The
implementations are reference implementations — adapt them to whatever
LLM gateway, deployment shape, or coordination scope you need.
