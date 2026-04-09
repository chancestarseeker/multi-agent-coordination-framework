# Multi-Agent Coordination Tech Stack

**For reviewing a hybrid scope of work (code + deliverables + project planning)**
**Grounded in: Foundations of Multi-AI-Agent Coordination v0.4**

---

## Stack Overview

| Layer | Technology | Role in Framework |
|-------|-----------|-------------------|
| **Ledger** | Git repo (JSON + Markdown) | Coordination Ledger (`fnd-ledger.md`) |
| **Orchestrator** | Python script using LiteLLM | Coordination Field (`fnd-field.md`) |
| **Agent Providers** | Claude, GPT, Gemini, Llama, etc. via LiteLLM | Participants (`fnd-participants.md`) |
| **Signal Transport** | Filesystem + Git commits | Signal Envelope (preamble) |
| **Circuit Breakers** | Python watchdog in orchestrator | Circuit Breakers (preamble) |
| **Human Interface** | CLI dashboard or simple web UI | Observer/Active participant |

---

## 1. The Ledger — Git Repository

**Why Git:** It natively satisfies all four ledger requirements from `fnd-ledger.md`:

- **Readable** by every participant — agents read files from the repo
- **Appendable** by every participant — new entries are appended to JSON files
- **Immutable** once written — Git history is append-only; entries are never edited
- **Durable** across sessions — the repo persists independently of any agent

### Repo Structure

```
coordination/
├── ledger/
│   ├── entries/              # One JSON file per entry, named by entry_id
│   │   ├── 001-decision-kickoff.json
│   │   ├── 002-boundary_change-claude-entry.json
│   │   ├── 003-attempt-code-review.json
│   │   └── ...
│   ├── summary.md            # Auto-generated ledger summary for context loading
│   └── index.json            # Entry index for scoped reads (scope → entry_ids)
│
├── participants/
│   ├── declarations/         # One JSON file per participant declaration
│   │   ├── claude-sonnet.json
│   │   ├── gpt-4o.json
│   │   ├── gemini-pro.json
│   │   └── human-reviewer.json
│   └── directory.json        # Coordination Directory (built from ledger history)
│
├── signal/
│   ├── inbox/                # Pending signal envelopes (consumed on read)
│   └── archive/              # Processed signals (for lineage tracing)
│
├── scope/                    # The actual work being reviewed
│   ├── code/                 # Codebase under review
│   ├── deliverables/         # Written documents under review
│   └── plan/                 # Project plan artifacts under review
│
├── foundations/               # Your fnd-*.md files (loaded into agent context as needed)
│   ├── fnd-preamble.md
│   ├── fnd-participants.md
│   ├── fnd-signal.md
│   ├── fnd-failure.md
│   ├── fnd-field.md
│   ├── fnd-ledger.md
│   └── fnd-repair.md
│
└── config.json               # Circuit breaker thresholds, mode, field configuration
```

### Entry File Example

```json
{
  "entry_id": "003",
  "timestamp": "2026-04-09T14:30:00Z",
  "author": "claude-sonnet",
  "type": "attempt",
  "scope": "code/auth-module",
  "prior_entries": ["001"],
  "summary": "Beginning security review of auth module. Focusing on token validation and session management. Prior entry 001 established this as high-priority scope.",
  "detail": "Reviewing src/auth/ for: injection vulnerabilities, token expiry handling, session fixation risks. Will cross-reference against OWASP top 10. Constraint: 8k token budget for this review pass.",
  "confidence": 0.8,
  "foundation_tag": ["intention", "boundaries"]
}
```

---

## 2. The Orchestrator — Python + LiteLLM

The orchestrator implements the Coordination Field (`fnd-field.md`). It is itself a participant with a declaration — not a supervisor.

### Why LiteLLM

LiteLLM provides a unified interface to call any LLM provider (Claude, GPT, Gemini, Llama, Mistral, etc.) with the same API shape. This lets you:

- Define participants by provider/model without coupling the orchestrator to any SDK
- Track token usage and cost per participant (feeds Balance foundation)
- Swap or add providers without changing orchestrator logic

### Core Orchestrator Responsibilities

```
┌─────────────────────────────────────────────────┐
│                  ORCHESTRATOR                     │
│                                                   │
│  1. Load participant declarations                 │
│  2. Load ledger summary into each agent's context │
│  3. Load relevant fnd-*.md modules per Module     │
│     Index rules (preamble)                        │
│  4. Construct signal envelopes for task proposals  │
│  5. Route tasks based on declarations + capacity   │
│  6. Monitor circuit breaker thresholds             │
│  7. Write ledger entries from agent responses       │
│  8. Generate ledger summaries between sessions      │
│                                                   │
│  Mode: starts Infrastructure, transitions to       │
│  Orchestrated when review tasks are routed         │
└─────────────────────────────────────────────────┘
```

### Key Dependencies

```
litellm          # Unified LLM API (Claude, GPT, Gemini, etc.)
pydantic         # Schema validation for ledger entries + signal envelopes
gitpython        # Programmatic git commits for ledger writes
watchdog         # File system monitoring (optional, for signal inbox)
rich             # CLI dashboard for human participant interface
```

---

## 3. Participant Design — Agents as Reviewers

Each agent is a participant with a declaration. For a scope-of-work review, you'd configure agents with complementary capabilities:

### Example Participant Roster

| Identifier | Provider/Model | Review Focus | Rationale |
|------------|---------------|--------------|-----------|
| `claude-opus` | Anthropic / Claude Opus | Architectural coherence, intention alignment | Strong at reasoning about systems, good at "does this serve the purpose" |
| `claude-sonnet` | Anthropic / Claude Sonnet | Code review, security, implementation detail | Fast, cost-effective for detailed technical passes |
| `gpt-4o` | OpenAI / GPT-4o | Deliverable quality, writing clarity, completeness | Different perspective on written artifacts; catches different things |
| `gemini-pro` | Google / Gemini 2.5 Pro | Project plan feasibility, risk identification | Long context window useful for cross-referencing large plans |
| `human-lead` | Human | Final judgment, intention setting, repair escalation | Sets coordination intention; receives escalations |

### Declaration File Example

```json
{
  "identifier": "claude-sonnet",
  "steward": "Anthropic",
  "version": "claude-sonnet-4-20250514",
  "capability_envelope": {
    "code_review": 0.85,
    "security_analysis": 0.75,
    "architectural_reasoning": 0.70,
    "writing_review": 0.60,
    "project_planning": 0.50
  },
  "preferred_tasks": ["code_review", "security_analysis"],
  "known_limitations": [
    "May miss business-context issues in project plans",
    "Token budget constrains review of very large files",
    "Cannot execute code to verify runtime behavior"
  ],
  "boundary_declaration": [
    "Will not approve code changes — review only",
    "Will not make financial or legal judgments"
  ],
  "availability": "on-demand, API-based, no rate limit concerns at expected volume",
  "signal_formats": {
    "preferred_input": "markdown with code blocks",
    "preferred_output": "structured JSON with markdown detail field",
    "max_payload_size": "180k tokens"
  },
  "context_constraints": {
    "context_window": 200000,
    "token_budget_per_task": 8000,
    "latency_tolerance_seconds": 120
  },
  "cost_model": {
    "type": "per_token",
    "input_cost_per_million": 3.0,
    "output_cost_per_million": 15.0,
    "currency": "USD"
  },
  "resource_ceiling": {
    "max_tokens_per_session": 500000,
    "max_cost_per_session_usd": 10.0
  },
  "participation_mode": "active",
  "capacity": "full"
}
```

---

## 4. Signal Transport — How Agents Communicate

Agents don't talk to each other directly. All communication flows through signal envelopes written to the filesystem and processed by the orchestrator.

### Signal Envelope (JSON)

```json
{
  "signal_id": "sig-007",
  "origin": "claude-sonnet",
  "destination": "orchestrator",
  "timestamp": "2026-04-09T14:35:00Z",
  "type": "state_update",
  "payload": {
    "proposed_entry": {
      "type": "completion",
      "scope": "code/auth-module",
      "summary": "Auth module review complete. Found 2 medium-severity issues...",
      "confidence": 0.82
    }
  },
  "context_summary": "Reviewed 12 files in src/auth/. Cross-referenced against attempt entry 003.",
  "confidence": 0.82,
  "lineage": ["sig-003", "sig-004"]
}
```

### Flow

```
Agent completes work
  → Constructs signal envelope (JSON)
  → Writes to signal/inbox/
  → Orchestrator picks up signal
  → Validates proposed ledger entry
  → Appends to ledger/entries/
  → Git commits the new entry
  → Routes next task or surfaces results
```

---

## 5. Circuit Breakers — Implementation

Defined in `config.json`, monitored by the orchestrator:

```json
{
  "mode": "orchestrated",
  "orchestrator": "human-lead",
  "circuit_breakers": {
    "timeout_seconds": 120,
    "conflict_detection": true,
    "resource_multiplier": 2.0,
    "confidence_floor": 0.3,
    "repetition_threshold": 3
  },
  "balance": {
    "track_tokens_per_participant": true,
    "track_cost_per_participant": true,
    "surface_imbalance_at_multiplier": 1.5
  }
}
```

The orchestrator checks these after every signal:

- **Timeout**: No acknowledgment within `latency_tolerance_seconds` from the participant's declaration
- **Conflict**: Two entries propose different state for the same `scope`
- **Resource**: Any participant exceeds `2×` the per-participant average in tokens or cost
- **Confidence**: A completion arrives with confidence < 0.3 and no fallback route exists
- **Repetition**: Same scope attempted 3+ times without a completion, failure, or repair advancing it

When a breaker fires → orchestrator writes a `failure` entry → loads `fnd-repair.md` into its own context → enters repair cycle.

---

## 6. The Review Workflow

Here's how the agents actually review your scope of work:

### Phase 1: Setup (Infrastructure Mode)

1. You (human participant) set the coordination intention
2. Place the scope of work in `scope/`
3. Configure participant declarations in `participants/declarations/`
4. Orchestrator writes a `decision` entry recording the intention and initial scope decomposition

### Phase 2: Decompose & Route (Orchestrated Mode)

The orchestrator decomposes the scope into reviewable units:

```
scope/code/auth-module       → claude-sonnet  (security focus)
scope/code/data-pipeline     → claude-sonnet  (implementation review)
scope/deliverables/proposal  → gpt-4o         (writing quality + completeness)
scope/deliverables/contract  → gpt-4o         (clarity + coverage)
scope/plan/timeline          → gemini-pro     (feasibility + risk)
scope/plan/resource-plan     → gemini-pro     (balance + dependencies)

Cross-cutting review:
  "Does the whole thing cohere?"  → claude-opus (intention alignment)
```

Each routing decision is a `decision` entry. Each acceptance is an `attempt` entry.

### Phase 3: Parallel Review (Orchestrated Mode)

Agents review in parallel. Each writes:
- Intermediate findings as `attempt` updates (frequent, incremental writes per `fnd-ledger.md`)
- Final review as a `completion` entry with confidence

### Phase 4: Synthesis (Orchestrated or Emergent Mode)

After individual reviews complete:
- Orchestrator surfaces all findings to `claude-opus` for synthesis
- Or: transition to Emergent Mode where agents read each other's completions and self-select follow-up questions
- Conflicts between reviewers (e.g., one flags a risk another dismissed) trigger the Conflict circuit breaker → repair cycle

### Phase 5: Human Decision (Orchestrated Mode)

- Synthesized review surfaces to `human-lead`
- Human accepts, requests deeper review on specific areas, or closes

---

## 7. Getting Started — Minimal Viable Coordination

You don't need to build all of this at once. Start with:

### Step 1: The ledger repo

```bash
mkdir coordination && cd coordination
git init
mkdir -p ledger/entries participants/declarations signal/{inbox,archive} scope foundations
# Copy your fnd-*.md files into foundations/
```

### Step 2: One orchestrator script

A Python script (~200-300 lines) that:
- Reads declarations from `participants/declarations/`
- Loads the right `fnd-*.md` files into each agent's system prompt
- Calls LiteLLM with the scope material + framework context
- Parses agent responses into ledger entries
- Appends entries and git-commits

### Step 3: Two agents reviewing one scope

Start with just two providers reviewing the same artifact. See where their findings converge and diverge. This is your first test of whether the signal envelope structure carries enough context.

### Step 4: Add circuit breakers and scale

Once the basic loop works, add threshold monitoring, then scale to the full roster.

---

## Alternative Ledger Options Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| **Git repo** (recommended) | Immutable history, diffable, works offline, free | Requires git operations per write | Best fit for append-only + durability |
| **SQLite file** | Fast queries, scoped reads easy | Not naturally append-only, less transparent | Good if query patterns get complex |
| **ClickUp** | You're already exploring it | API overhead, not append-only, rate limits | Better as a human-facing view *on top of* the ledger, not as the ledger itself |
| **Shared markdown file** | Simplest possible | Merge conflicts at scale, no structured queries | Fine for 2-3 agents, breaks at 5+ |

ClickUp could still serve as the **human participant's interface** — a dashboard that reads from the git ledger and presents findings, routes decisions back. But the source of truth should be the git repo.
