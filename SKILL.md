---
name: agent-coordination
description: Multi-AI-agent coordination grounded in the Foundations of Multi-AI-Agent Coordination v0.4 — six commitments (Choice, Boundaries, Truth, Balance, Recursion, Intention) with a shared append-only ledger. Use when coordinating multiple LLMs to review code/deliverables/plans, when diagnosing multi-agent failures, or when deploying coordinated agents to Hermes Agent by Nous Research.
---

# Agent Coordination

A skill for coordinating multiple AI agents under a six-foundation
framework that treats agents as peer participants rather than
subordinates in a hierarchy.

## When to invoke this skill

- The user asks for **multi-agent review** of an artifact (code,
  deliverable, plan) where multiple LLMs should form independent
  judgments and surface conflicts
- The user asks to **diagnose a multi-agent failure** — agents
  disagreeing, drifting from intent, repeated failures on the same scope,
  agents being overridden when they refuse
- The user asks to **deploy a coordinated agent system** to Hermes Agent
  by Nous Research, or to any LLM gateway with multiple providers
- The user references **the foundations** (Choice, Boundaries, Truth,
  Balance, Recursion, Intention) or asks how to honor any of them in
  practice

## Files in this skill

```
agent-coordination/
├── SKILL.md                          ← you are here
├── README.md                          ← project overview for humans
├── coordination-tech-stack.md         ← original blueprint
├── foundations/                        ← canonical framework specification
│   └── fnd-*.md (7 files)
├── cli/                                ← single-process Python CLI
│   ├── README.md                       full setup + command reference
│   ├── orchestrator.py                 reference implementation
│   ├── config.json, requirements.txt
│   ├── participants/declarations/      example participant declarations
│   └── scope/code/example_auth.py      example artifact for review
└── hermes/                             ← daemon + Hermes Agent skill
    ├── README.md                       hermes deployment overview
    ├── SKILL.md                        loaded by Hermes Agent itself
    ├── CLAUDE.md                       quick reference card
    ├── daemon.py                       HTTP service on :8420
    ├── coordination.yaml               daemon config
    ├── participants.yaml               model declarations with hermes routing
    └── *.schema.json                   JSON schemas for validation
```

## Operating contract

**Step 1 — Always load `foundations/fnd-preamble.md` first.** It defines
the six foundations, the signal envelope structure, the circuit breaker
thresholds, and the **Module Index** that tells you which other
`fnd-*.md` files to load on which conditions. Do NOT load all foundations
files preemptively — the Module Index is explicit about lazy loading.

**Step 2 — Choose the implementation path that fits the request.**

| Situation | Path |
|---|---|
| Local testing, demos, exploring the framework hands-on, single-process | `cli/` — read `cli/README.md` for setup and command reference, then run `cli/orchestrator.py` |
| Production deployment, multiple Hermes instances, persistent service | `hermes/` — read `hermes/README.md` for the daemon + skill installation, then start the daemon |
| Just answering a framework question | `foundations/` only — no implementation needed |

**Step 3 — Honor the framework invariants.** These are not
mode-dependent. They are the architecture's load-bearing commitments. Do
NOT under any circumstances:

1. **Override a participant's refusal.** A `type=failure` response is
   signal, not malfunction. Route to an alternative; never retry the
   same participant after refusal.
2. **Suppress or modify a confidence report.** If a participant says
   `confidence: 0.4`, that is the signal. Surfacing weak confidence is
   the Truth foundation.
3. **Write to the ledger on behalf of a participant without their
   signal.** The orchestrator validates and persists; it does not author
   on others' behalf. The participant's signal IS their authorization.
4. **Route around a declared boundary.** `boundary_declaration` fields
   are absolute. The framework distinguishes mechanical enforcement
   (runtime-enforced) from self-declared (commitment without enforcement)
   via the `enforcement` field — both are valid; mechanical is stronger
   signal.
5. **Continue after a circuit breaker fires** without entering the repair
   cycle. A system that ignores breakers has chosen speed over integrity
   and will compound the failure downstream.
6. **Inject your context into a subagent's prompt** beyond a structured
   signal envelope. This is context contamination — the most common
   boundary failure in multi-agent systems.

These invariants apply equally when you (Claude) are operating as a
participant in the coordination, when you are advising the user about
how to set up coordination, and when you are the orchestrator role-holder
in any capacity.

## Quick command reference (CLI implementation)

```bash
cd cli
python orchestrator.py take-role --scope <path> --as <participant>
python orchestrator.py review --scope <path>
python orchestrator.py repair --failure-entry <id> --arbiter <participant>
python orchestrator.py synthesize --scope <path>
python orchestrator.py self-select --scope <path> --as <participant>
python orchestrator.py release-role --scope <path> --as <participant>
python orchestrator.py inbox process
python orchestrator.py ledger
```

Full reference and explanations: `cli/README.md`.

## Quick command reference (hermes deployment)

```bash
cd hermes
python daemon.py --config coordination.yaml
# in another shell:
curl http://localhost:8420/health
curl http://localhost:8420/registry
curl "http://localhost:8420/select?capability=code_generation&max_cost=0.01"
```

Full reference and architecture: `hermes/README.md`.
