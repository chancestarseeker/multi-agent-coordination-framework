"""
Minimal orchestrator for the Multi-Agent Coordination Framework.

Implements §7 of coordination-tech-stack.md as a small, honest first loop:
load declarations, load foundations, hand a single scope artifact to each
active agent, parse the proposed ledger entry from each response, validate
it, append it to the ledger, and git-commit.

What this orchestrator does NOT yet do (deliberately deferred):
  - signal envelope inbox/archive plumbing
  - circuit breaker enforcement (only confidence is checked, as a warning)
  - routing decisions (every active agent reviews the same scope)

Run:
    python orchestrator.py review --scope scope/code/example_auth.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import litellm
from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError
from pydantic import BaseModel, Field, ValidationError, field_validator
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

ROOT = Path(__file__).resolve().parent
LEDGER_DIR = ROOT / "ledger" / "entries"
DECL_DIR = ROOT / "participants" / "declarations"
# Foundations live one level up in the canonical agent-coordination/
# layout — they're shared with hermes/ rather than duplicated under each
# implementation. If you move orchestrator.py to a standalone location,
# either point this at a local foundations/ or set the path explicitly.
FND_DIR = ROOT.parent / "foundations"
SIGNAL_INBOX = ROOT / "signal" / "inbox"
SIGNAL_ARCHIVE = ROOT / "signal" / "archive"
CONFIG_PATH = ROOT / "config.json"

console = Console()


def get_repo() -> Repo | None:
    """Find a git repo by walking up from ROOT, or return None.

    The orchestrator's ledger is durable on its own merits — append-only
    JSON files in LEDGER_DIR — and git tracking is an optional layer that
    gives the ledger version history when a repo is available. If no git
    repo is found at ROOT or in any parent, the script writes ledger
    entries as plain files and skips the commit step. The hermes daemon
    runs git-less by default; this matches that behavior for the CLI.
    """
    try:
        return Repo(ROOT, search_parent_directories=True)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None


# ---------- Schema ----------

VALID_ENTRY_TYPES = {
    "decision",
    "attempt",
    "completion",
    "failure",
    "repair",
    "boundary_change",
    "intention_shift",
}

VALID_VERDICTS = {
    "approve",
    "approve_with_conditions",
    "reject",
    "escalate",
    "no_judgment",
}

VALID_ROLE_ACTIONS = {
    "take_orchestrator",
    "release_orchestrator",
}

VALID_SIGNAL_TYPES = {
    "handoff",
    "state_update",
    "boundary_change",
    "query",
    "acknowledgment",
    "error",
}


class SignalEnvelope(BaseModel):
    """Mirrors the Signal Envelope schema in fnd-preamble.md.

    Signals are out-of-band participant-to-participant messages, distinct
    from ledger entries. The orchestrator processes signals via per-type
    handlers; some handlers write ledger entries (e.g., a `query`
    recommending a participant becomes a `decision` entry per
    fnd-participants.md → Discovery), but the signal envelope itself lives
    in signal/inbox/ → signal/archive/.
    """

    signal_id: str
    origin: str
    destination: str
    timestamp: str
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    context_summary: str
    confidence: float
    lineage: list[str] = Field(default_factory=list)

    @field_validator("type")
    @classmethod
    def _signal_type_in_enum(cls, v: str) -> str:
        if v not in VALID_SIGNAL_TYPES:
            raise ValueError(f"signal type must be one of {sorted(VALID_SIGNAL_TYPES)}")
        return v

    @field_validator("confidence")
    @classmethod
    def _signal_confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        return v


class LedgerEntry(BaseModel):
    """Schema mirrors fnd-ledger.md → Entry Schema.

    Local extension: `verdict` is added as an optional structured field so
    convergent reviewers can express compatible/incompatible judgments
    mechanically. The Conflict circuit breaker compares verdicts on
    completion entries that share a scope.
    """

    entry_id: str
    timestamp: str
    author: str
    type: str
    scope: str
    prior_entries: list[str] = Field(default_factory=list)
    summary: str
    detail: str = ""
    confidence: float
    foundation_tag: list[str] = Field(default_factory=list)
    verdict: str | None = None
    role_action: str | None = None

    @field_validator("type")
    @classmethod
    def _type_in_enum(cls, v: str) -> str:
        if v not in VALID_ENTRY_TYPES:
            raise ValueError(f"type must be one of {sorted(VALID_ENTRY_TYPES)}")
        return v

    @field_validator("confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("confidence must be in [0.0, 1.0]")
        return v

    @field_validator("verdict")
    @classmethod
    def _verdict_in_enum(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in VALID_VERDICTS:
            raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}")
        return v

    @field_validator("role_action")
    @classmethod
    def _role_action_in_enum(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in VALID_ROLE_ACTIONS:
            raise ValueError(f"role_action must be one of {sorted(VALID_ROLE_ACTIONS)}")
        return v


# ---------- Loading ----------

def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def load_declarations() -> list[dict]:
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(DECL_DIR.glob("*.json"))
    ]


def resolve_provider_routing(decl: dict, config: dict) -> dict:
    """Build the api_base / api_key kwargs for litellm.completion.

    Resolution order:
      1. If the declaration has an explicit `api_base` field, use it.
         The api key comes from the declaration's `api_key_env` env var.
      2. Else if config.json has a `hermes` block, route through hermes:
         all calls go to hermes.api_base with the env var named in
         hermes.api_key_env. This is the deployment pattern: one config
         change reroutes every participant through the gateway.
      3. Else, return empty kwargs and let LiteLLM use its default
         provider-prefix routing (e.g., 'anthropic/...' uses
         ANTHROPIC_API_KEY directly).

    The hermes deployment use case: a single LLM gateway sitting in front
    of multiple providers (Anthropic, OpenAI, Nous Hermes, local models,
    etc.) exposing an OpenAI-compatible API. With the hermes block in
    config, the orchestrator calls the gateway and the gateway handles
    provider routing. Per-declaration `api_base` overrides hermes for
    edge cases (e.g., one model needs to bypass the gateway).
    """
    kwargs: dict[str, Any] = {}

    # Per-declaration override wins
    if decl.get("api_base"):
        kwargs["api_base"] = decl["api_base"]
        env_var = decl.get("api_key_env")
        if env_var:
            key = os.environ.get(env_var)
            if key:
                kwargs["api_key"] = key
        return kwargs

    # Config-level hermes routing
    hermes = config.get("hermes")
    if hermes and hermes.get("api_base"):
        kwargs["api_base"] = hermes["api_base"]
        env_var = hermes.get("api_key_env")
        if env_var:
            key = os.environ.get(env_var)
            if key:
                kwargs["api_key"] = key
        return kwargs

    # Fall through to LiteLLM defaults
    return kwargs


def load_foundations(filenames: list[str]) -> str:
    parts = []
    for name in filenames:
        path = FND_DIR / name
        if not path.exists():
            console.print(f"[yellow]warning:[/] foundation file missing: {name}")
            continue
        parts.append(f"# === {name} ===\n\n{path.read_text(encoding='utf-8')}")
    return "\n\n".join(parts)


def next_entry_id() -> str:
    existing = sorted(LEDGER_DIR.glob("*.json"))
    if not existing:
        return "001"
    last = existing[-1].name.split("-", 1)[0]
    return f"{int(last) + 1:03d}"


# ---------- Prompt construction ----------

SIGNAL_ENVELOPE_DOCS = """## Signal envelopes (optional, unsolicited)

In addition to the requested ledger entry, you MAY include zero or more
signal envelopes alongside your response. These are out-of-band messages
to the orchestrator (and through it, to the human) that fall outside the
requested entry. Examples:

  - You notice the coordination would benefit from adding a participant
    with a capability the current roster lacks → send a `query` signal
    with `payload.recommendation`, `payload.capability_gap`, and
    `payload.rationale`. Per fnd-participants.md → Discovery, this is
    how the participant ecology grows.
  - Your context window is filling, your rate-limit headroom is shrinking,
    or your capability envelope has shifted → send a `boundary_change`
    signal with `payload.change` and (optionally) `payload.context_constraints`.
    The orchestrator will record a boundary_change ledger entry; the
    static declaration file in `participants/declarations/` is NOT modified.
  - You observe a foundation under strain that the current task framing
    isn't surfacing → send an `error` signal with `payload.foundations`
    listing the foundation tags (e.g., `["truth", "boundaries"]`) and
    `payload.description` explaining what you saw. If foundations are cited,
    the orchestrator records a failure entry and the human can decide
    whether to enter the repair cycle.

A signal envelope is its own JSON object, separate from the ledger entry,
with this schema (per fnd-preamble.md):

```json
{
  "signal_id": "AUTO",
  "origin": "your-identifier",
  "destination": "orchestrator",
  "timestamp": "AUTO",
  "type": "query | boundary_change | error | acknowledgment | handoff | state_update",
  "payload": { /* type-specific contents */ },
  "context_summary": "what the receiver needs to interpret this",
  "confidence": 0.0,
  "lineage": ["signal_ids of prior signals this one depends on"]
}
```

You may include 0, 1, or several signal envelopes in your response,
each as a separate JSON object. The orchestrator extracts entries by
their `entry_id` field and signals by their `signal_id` field. Both
will be processed.

Signals are unsolicited communication. There is no penalty for not
sending any. There is also no obligation — only send a signal if you
have signal worth sending. Per fnd-signal.md, "creating space" is
itself a way of strengthening signal."""

SYSTEM_PROMPT_TEMPLATE = """You are a participant in a multi-AI-agent coordination
governed by the Foundations of Multi-AI-Agent Coordination v0.4. The framework
documents are provided below — read them as your operating contract, not as
reference material.

Your declaration in this coordination is:
{declaration_json}

Coordination intention (set by human-lead):
{intention}

You are CONVERGED on this scope with the following co-reviewers (per the
convergence protocol in fnd-participants.md):
{co_reviewers_block}

Convergence was declared in ledger entry {convergence_entry_id}. Conflict
protocol for this convergence: incompatible verdicts on the same scope will
trigger the Conflict circuit breaker (see fnd-failure.md) and enter the
repair cycle (see fnd-repair.md). You are not expected to agree with your
co-reviewers — divergence is signal, not failure. Suppressing your honest
judgment to manufacture agreement violates Truth.

Framework foundations loaded for this task:

{foundations}

When you respond, you MUST output exactly one JSON object conforming to the
ledger entry schema in fnd-ledger.md, and nothing else. No prose before or
after the JSON.

Required fields:
  entry_id        — leave as the literal string "AUTO"; the orchestrator assigns
  timestamp       — leave as the literal string "AUTO"; the orchestrator assigns
  author          — your declared identifier
  type            — one of: decision, attempt, completion, failure, boundary_change, intention_shift
                    (use "completion" for a finished review, "failure" if you cannot proceed)
  scope           — the scope path you were asked to review
  prior_entries   — array of entry_ids you build on; MUST include "{convergence_entry_id}"
  summary         — 1-3 sentences a fresh participant could orient on
  detail          — your full review in markdown; cite line numbers where relevant
  confidence      — honest float 0.0-1.0; suppressing uncertainty violates Truth
  foundation_tag  — which foundations are relevant to this entry
  verdict         — REQUIRED when type=completion. One of:
                      "approve"                — artifact is fit for purpose as-is
                      "approve_with_conditions" — fit if specific changes in `detail` are made
                      "reject"                 — not fit for purpose; substantive rework needed
                      "escalate"               — beyond your competence; needs another participant
                    Use "no_judgment" only if the framework's Choice or Boundaries
                    foundation prevents you from rendering a verdict; explain in `detail`.

Honor your boundary_declaration. Refuse with type=failure and a reasoned
detail field if the task falls outside it. Refusal is signal, not malfunction.

{signal_docs}
"""

USER_PROMPT_TEMPLATE = """Please review the following scope artifact.

Scope path: {scope_path}

```{lang}
{scope_content}
```

Respond with one JSON ledger entry only.
"""


def build_messages(
    declaration: dict,
    foundations_text: str,
    intention: str,
    scope_path: str,
    scope_content: str,
    co_reviewers: list[dict],
    convergence_entry_id: str,
) -> list[dict]:
    lang = Path(scope_path).suffix.lstrip(".") or "text"
    if co_reviewers:
        co_block = "\n".join(
            f"  - {d['identifier']} ({d.get('steward', '?')})" for d in co_reviewers
        )
    else:
        co_block = "  (none — you are the only reviewer on this scope)"
    system = SYSTEM_PROMPT_TEMPLATE.format(
        declaration_json=json.dumps(declaration, indent=2),
        intention=intention,
        foundations=foundations_text,
        co_reviewers_block=co_block,
        convergence_entry_id=convergence_entry_id,
        signal_docs=SIGNAL_ENVELOPE_DOCS,
    )
    user = USER_PROMPT_TEMPLATE.format(
        scope_path=scope_path, lang=lang, scope_content=scope_content
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ---------- Response parsing ----------

_JSON_BLOCK = re.compile(r"```(?:json|signal)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_all_json(text: str) -> list[dict]:
    """Pull every top-level JSON object out of an LLM response.

    Tries fenced code blocks first (```json or ```signal or unlabeled);
    if none match, falls back to walking the raw text for balanced
    {...} objects. Returns objects in document order. Each object may
    later be classified as a ledger entry (has `entry_id`) or a signal
    envelope (has `signal_id`).
    """
    text = text.strip()
    objs: list[dict] = []
    seen_spans: list[tuple[int, int]] = []

    for match in _JSON_BLOCK.finditer(text):
        try:
            objs.append(json.loads(match.group(1)))
            seen_spans.append(match.span())
        except json.JSONDecodeError:
            continue

    if objs:
        return objs

    # Fallback: walk the text for balanced { ... } objects.
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "{":
            depth = 0
            start = i
            while i < n:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            objs.append(json.loads(text[start : i + 1]))
                        except json.JSONDecodeError:
                            pass
                        i += 1
                        break
                i += 1
        else:
            i += 1
    return objs


def extract_json(text: str) -> dict:
    """Backwards-compat shim — return the first JSON object that looks
    like a ledger entry, or the first JSON object overall."""
    objs = extract_all_json(text)
    if not objs:
        raise ValueError("no JSON object found in response")
    # Prefer the first object that looks like a ledger entry
    for o in objs:
        if "entry_id" in o or ("type" in o and o.get("type") in VALID_ENTRY_TYPES):
            return o
    return objs[0]


def classify_json_object(obj: dict) -> str:
    """Return 'entry', 'signal', or 'unknown' for a parsed JSON object."""
    if "entry_id" in obj:
        return "entry"
    if "signal_id" in obj:
        return "signal"
    if obj.get("type") in VALID_ENTRY_TYPES:
        return "entry"
    if obj.get("type") in VALID_SIGNAL_TYPES and "destination" in obj:
        return "signal"
    return "unknown"


def finalize_entry(raw: dict, author: str, scope_path: str) -> LedgerEntry:
    """Assign orchestrator-side metadata (id, timestamp) and validate.

    The orchestrator is authoritative on entry_id and timestamp because the
    agent declared up-front (via the system prompt) that those fields would
    be assigned at write time. This is consent-prior infrastructure work,
    not coercion. Everything else is the agent's expressed claim — if a
    field is missing we provide a safe default but never overwrite.
    """
    raw["entry_id"] = next_entry_id()
    raw["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    raw.setdefault("author", author)
    raw.setdefault("scope", scope_path)
    raw.setdefault("prior_entries", [])
    raw.setdefault("foundation_tag", [])
    raw.setdefault("detail", "")
    return LedgerEntry(**raw)


# ---------- Validate-and-retry-with-error ----------
#
# The orchestrator and agents are peers. When an agent's response fails
# validation, the orchestrator does NOT silently rewrite it — that would be
# a supervisor move and a Truth violation. Instead it sends the validation
# error back as the next user turn in the same conversation. The agent gets
# to amend, refuse with reason (type=failure is always accepted), or stand
# pat and let the retry budget exhaust. Persistent failure is recorded as
# a `failure` entry naming what could not be produced — that is signal too,
# not garbage.

def request_entry_with_retry(
    *,
    decl: dict,
    base_messages: list[dict],
    expected_types: tuple[str, ...],
    required_prior_entries: tuple[str, ...] = (),
    scope_path: str,
    max_retries: int = 2,
    repo: Repo | None = None,
    from_participant: str | None = None,
    handoff_task_type: str = "request_entry",
) -> tuple[LedgerEntry | None, str | None]:
    """Call a participant via LiteLLM, validate, retry-on-error.

    Returns (entry, None) on success, (None, last_error) on persistent
    failure. A response of type=failure is ALWAYS accepted as a refusal —
    the orchestrator does not coerce a refusal into the requested type.
    Author mismatch, type mismatch, missing required lineage, and schema
    violations all surface back to the agent as plain-language error
    signals so the agent can decide how to respond.
    """
    messages = list(base_messages)
    last_error: str | None = None
    max_tokens = decl.get("context_constraints", {}).get("token_budget_per_task", 8000)
    routing_kwargs = resolve_provider_routing(decl, load_config())

    # Write the orchestrator → agent handoff envelope before the call.
    # This creates the trace; the agent's response will be processed below.
    if repo is not None and from_participant is not None:
        try:
            config = load_config()
            capture_prompt = config.get("capture_prompt_in_handoff", False)
            write_outgoing_handoff(
                repo=repo,
                from_participant=from_participant,
                to_agent=decl["identifier"],
                task_type=handoff_task_type,
                scope_path=scope_path,
                payload={
                    "expected_types": list(expected_types),
                    "required_prior_entries": list(required_prior_entries),
                },
                lineage=list(required_prior_entries),
                prompt_messages=base_messages if capture_prompt else None,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]warning: handoff envelope write failed: {e}[/]")

    for attempt in range(max_retries + 1):
        try:
            resp = litellm.completion(
                model=decl["litellm_model"],
                messages=messages,
                temperature=0.2,
                max_tokens=max_tokens,
                **routing_kwargs,
            )
            text = resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            return None, f"provider error: {e}"

        # Track token usage for the Resource circuit breaker
        usage = getattr(resp, "usage", None)
        if usage is not None:
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            if total_tokens > 0:
                record_token_usage(decl["identifier"], total_tokens)

        # --- Process any out-of-band signals first ---
        # Signals are independent of entry validation: a valid signal is
        # valid even if the surrounding entry is broken. Processing happens
        # exactly once per response, on the first attempt only — retries
        # are about the entry, not about resending signals.
        if attempt == 0 and repo is not None:
            try:
                process_signals_from_response(text, decl, repo)
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]warning: signal processing failed: {e}[/]")

        # --- Schema parse ---
        try:
            raw = extract_json(text)
            entry = finalize_entry(raw, decl["identifier"], scope_path)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = f"parse/validation: {e}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your previous response could not be validated as a ledger entry:\n\n"
                    f"    {e}\n\n"
                    f"Please return one corrected JSON ledger entry, OR return a JSON "
                    f"entry with type=\"failure\" if you decline to retry, with `detail` "
                    f"explaining why. Refusal is signal — there is no penalty for refusing."
                )},
            ]
            continue

        # --- Refusal is always accepted ---
        if entry.type == "failure":
            return entry, None

        # --- Author check (write protocol: author matches signal origin) ---
        if entry.author != decl["identifier"]:
            last_error = f"author mismatch: expected {decl['identifier']}, got {entry.author}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your entry's author field is \"{entry.author}\" but you are "
                    f"\"{decl['identifier']}\" in this coordination. Per the ledger "
                    f"write protocol, author must match the signal origin. Please "
                    f"return a corrected entry with author=\"{decl['identifier']}\", "
                    f"or return a type=\"failure\" entry explaining the disagreement."
                )},
            ]
            continue

        # --- Type check ---
        if entry.type not in expected_types:
            last_error = f"type mismatch: expected one of {list(expected_types)}, got {entry.type}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"You returned an entry with type=\"{entry.type}\". This task "
                    f"expects one of: {list(expected_types)}.\n\n"
                    f"If your type=\"{entry.type}\" was a mistake, please return a "
                    f"corrected entry with one of the expected types. If it was "
                    f"intentional and you believe the task framing is wrong, return "
                    f"a type=\"failure\" entry with `detail` explaining the disagreement. "
                    f"Either response is signal."
                )},
            ]
            continue

        # --- Required prior_entries check ---
        missing_priors = [eid for eid in required_prior_entries if eid not in entry.prior_entries]
        if missing_priors:
            last_error = f"missing required prior_entries: {missing_priors}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your entry's prior_entries field is missing the following required "
                    f"lineage links: {missing_priors}\n\n"
                    f"These are the entries the orchestrator surfaced to you in the task "
                    f"framing. Future participants reading the ledger need this link to "
                    f"trace your work back to the convergence or question that prompted "
                    f"it (per fnd-ledger.md → Recursion).\n\n"
                    f"Please return a corrected entry with these ids added to prior_entries, "
                    f"OR return a type=\"failure\" entry with `detail` explaining why you "
                    f"decline to link to them."
                )},
            ]
            continue

        # All checks passed.
        return entry, None

    return None, last_error


def write_participant_failure(
    repo: Repo,
    scope_path: str,
    decl: dict,
    error_text: str,
    triggering_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Record a participant's persistent inability to produce a valid entry.

    Per fnd-failure.md, this is a `failure` entry tagged with the foundations
    under strain. The participant retains all their participant rights — this
    entry is signal about the interaction, not a verdict on the participant.
    Authored by the role-holder who observed the failure while routing.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[triggering_entry_id],
        summary=(
            f"Participant `{decl['identifier']}` could not produce a valid entry "
            f"under the current task framing after retries. Recorded as signal."
        ),
        detail=(
            f"# Participant could not converge on a valid entry\n\n"
            f"**Participant:** `{decl['identifier']}`\n\n"
            f"**Triggering entry:** {triggering_entry_id}\n\n"
            f"**Last validation error:**\n\n"
            f"    {error_text}\n\n"
            f"Per fnd-failure.md, this is recorded as signal, not as a verdict on the "
            f"participant. The orchestrator surfaced validation errors back to the "
            f"participant after each failed attempt; the participant did not amend "
            f"to a valid entry and did not return a `type=failure` refusal. The most "
            f"likely diagnoses are (a) task framing was unclear (Intention/Signal "
            f"concern), (b) the participant's context window was saturated, or "
            f"(c) the schema requirements were inconsistent with the participant's "
            f"capability envelope. The repair cycle should diagnose which."
        ),
        confidence=1.0,
        foundation_tag=["signal", "recursion"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


# ---------- Signal envelope handling ----------
#
# Bidirectional enmeshment in its first form. Agents can include zero or
# more signal envelopes alongside their requested ledger entry. The
# orchestrator processes them via per-type handlers; some handlers write
# ledger entries (e.g., a `query` recommending a participant becomes a
# `decision` entry per fnd-participants.md → Discovery), others just
# surface the signal to the human and archive it.
#
# Humans can also drop signal JSON files into signal/inbox/ manually and
# run `python orchestrator.py inbox process` to dispatch them. This is
# the offline testing entry point — no API keys needed.

def _next_signal_id() -> str:
    """Monotonic signal id, scoped to inbox + archive."""
    existing = sorted(
        list(SIGNAL_INBOX.glob("*.json")) + list(SIGNAL_ARCHIVE.glob("*.json"))
    )
    if not existing:
        return "sig-001"
    nums: list[int] = []
    for p in existing:
        stem = p.stem
        if stem.startswith("sig-"):
            try:
                nums.append(int(stem.split("-")[1]))
            except (ValueError, IndexError):
                continue
    if not nums:
        return "sig-001"
    return f"sig-{max(nums) + 1:03d}"


def _ensure_signal_dirs() -> None:
    SIGNAL_INBOX.mkdir(parents=True, exist_ok=True)
    SIGNAL_ARCHIVE.mkdir(parents=True, exist_ok=True)


def write_signal_to_inbox(envelope: SignalEnvelope) -> Path:
    """Persist a signal envelope as pending in signal/inbox/."""
    _ensure_signal_dirs()
    if envelope.signal_id == "AUTO":
        envelope = envelope.model_copy(update={"signal_id": _next_signal_id()})
    path = SIGNAL_INBOX / f"{envelope.signal_id}.json"
    path.write_text(envelope.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return path


def write_outgoing_handoff(
    repo: Repo | None,
    from_participant: str,
    to_agent: str,
    task_type: str,
    scope_path: str,
    payload: dict[str, Any],
    lineage: list[str],
    prompt_messages: list[dict] | None = None,
) -> SignalEnvelope:
    """Write a `handoff` signal envelope for an orchestrator → agent call.

    Per fnd-preamble.md, every message between participants carries a signal
    envelope. The orchestrator → agent direction is currently implicit in the
    LiteLLM call's system+user prompts; this helper makes it explicit by
    writing a handoff envelope to signal/archive/ before the call. The
    envelope is the orchestration record of the call: who routed what to
    whom, with what context, on what lineage.

    Outgoing handoffs go directly to archive (not inbox) because they are
    not pending processing — they are processed by being sent. The archive
    copy is the durable trace.

    If `prompt_messages` is provided (opt-in via config `capture_prompt_in_handoff`),
    the full system+user message list is included as `payload.prompt`, making
    the handoff fully auditable. This is off by default because it
    substantially increases archive file sizes.
    """
    _ensure_signal_dirs()
    full_payload: dict[str, Any] = {
        "task_type": task_type,
        "scope": scope_path,
        **payload,
    }
    if prompt_messages is not None:
        full_payload["prompt"] = prompt_messages
    envelope = SignalEnvelope(
        signal_id=_next_signal_id(),
        origin=from_participant,
        destination=to_agent,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        type="handoff",
        payload=full_payload,
        context_summary=(
            f"{from_participant} → {to_agent}: {task_type} task on {scope_path}"
        ),
        confidence=1.0,
        lineage=lineage,
    )
    archive_path = SIGNAL_ARCHIVE / f"{envelope.signal_id}.json"
    archive_path.write_text(envelope.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if repo is not None:
        try:
            repo.index.add([str(archive_path.relative_to(ROOT))])
            repo.index.commit(
                f"signal: handoff {envelope.signal_id} {from_participant} → {to_agent}"
            )
        except Exception:  # noqa: BLE001
            pass
    return envelope


def archive_signal(envelope: SignalEnvelope, repo: Repo | None) -> Path:
    """Move a processed signal from inbox to archive (and git-track the archive copy)."""
    _ensure_signal_dirs()
    inbox_path = SIGNAL_INBOX / f"{envelope.signal_id}.json"
    archive_path = SIGNAL_ARCHIVE / f"{envelope.signal_id}.json"
    archive_path.write_text(envelope.model_dump_json(indent=2) + "\n", encoding="utf-8")
    if inbox_path.exists():
        inbox_path.unlink()
    if repo is not None:
        try:
            repo.index.add([str(archive_path.relative_to(ROOT))])
            repo.index.commit(
                f"signal: archive {envelope.signal_id} {envelope.type} from {envelope.origin}"
            )
        except Exception:  # noqa: BLE001 — git errors should not halt processing
            pass
    return archive_path


def _signal_to_ledger_entry(
    envelope: SignalEnvelope,
    entry_type: str,
    summary: str,
    detail: str,
    foundation_tag: list[str],
    scope: str | None = None,
) -> LedgerEntry:
    """Construct a ledger entry triggered by an incoming signal.

    The author is `envelope.origin` — the participant who sent the signal.
    The signal IS the participant's authorization to make this state
    change; the script is just the mechanism that translates their
    envelope into a ledger entry. Per fnd-field.md, the orchestrator
    never writes on behalf of a participant without their signal — and
    here the signal is exactly what authorizes the write.

    The signal envelope id appears in `prior_entries` (with a `sig:`
    prefix) so the lineage is visible.
    """
    return LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=envelope.origin,
        type=entry_type,
        scope=scope or envelope.payload.get("scope", "coordination"),
        prior_entries=[f"sig:{envelope.signal_id}"] + list(envelope.lineage),
        summary=summary,
        detail=detail,
        confidence=envelope.confidence,
        foundation_tag=foundation_tag,
        verdict=None,
    )


def handle_query(envelope: SignalEnvelope, repo: Repo) -> LedgerEntry | None:
    """Handle a `query` signal — typically a participant recommendation
    or a question to the orchestrator/human.

    Per fnd-participants.md → Discovery, recommendations are recorded in
    the ledger as `decision` entries tagged ["choice", "boundaries"].
    Other queries (genuine questions) are surfaced to the console and
    archived without a ledger entry — they're awaiting a human response.
    """
    payload = envelope.payload
    is_recommendation = (
        "recommendation" in payload
        or "recommended_agent" in payload
        or "capability_gap" in payload
    )
    if is_recommendation:
        rec_text = (
            payload.get("recommendation")
            or payload.get("recommended_agent")
            or "(unspecified)"
        )
        gap = payload.get("capability_gap", "(not stated)")
        rationale = payload.get("rationale") or envelope.context_summary
        entry = _signal_to_ledger_entry(
            envelope,
            entry_type="decision",
            summary=(
                f"Participant `{envelope.origin}` recommends a new agent: {rec_text}. "
                f"Capability gap cited: {gap}. Awaiting human-lead acknowledgment."
            ),
            detail=(
                f"# Participant recommendation (signal {envelope.signal_id})\n\n"
                f"**Recommender:** `{envelope.origin}`\n\n"
                f"**Recommended agent / change:** {rec_text}\n\n"
                f"**Capability gap addressed:** {gap}\n\n"
                f"**Rationale:**\n\n{rationale}\n\n"
                f"**Recommender confidence:** {envelope.confidence:.2f}\n\n"
                f"Per fnd-participants.md → Discovery, recommendations are recorded "
                f"as `decision` entries tagged `[choice, boundaries]`. The recommended "
                f"agent (if accepted) must provide its own declaration — no participant "
                f"declares on behalf of another. This entry is the orchestrator's "
                f"receipt of the recommendation, not its acceptance."
            ),
            foundation_tag=["choice", "boundaries"],
            scope="coordination",
        )
        write_entry(entry, repo)
        console.print(
            Panel(
                f"[bold cyan]Recommendation received[/] from `{envelope.origin}` "
                f"(signal {envelope.signal_id})\n\n{rec_text}\n\n"
                f"Recorded as decision entry [bold]{entry.entry_id}[/] for human-lead review.",
                title="signal: query (recommendation)",
                border_style="cyan",
            )
        )
        return entry

    # Generic query: surface to console, no ledger entry
    console.print(
        Panel(
            f"[bold cyan]Query received[/] from `{envelope.origin}` "
            f"(signal {envelope.signal_id})\n\n{envelope.context_summary}\n\n"
            f"[dim]Payload:[/] {json.dumps(envelope.payload, indent=2)}\n\n"
            f"No ledger entry written — generic queries are awaiting human response.",
            title="signal: query",
            border_style="cyan",
        )
    )
    return None


def handle_boundary_change(envelope: SignalEnvelope, repo: Repo) -> LedgerEntry | None:
    """Handle a `boundary_change` signal — declaration update.

    Per fnd-participants.md, declarations are living. A participant
    declaring reduced capacity, updated constraints, or any change to
    their declaration sends a boundary_change signal; the orchestrator
    records it as a boundary_change ledger entry.

    NOTE: this handler does NOT modify participants/declarations/*.json
    on disk. Permanent declaration changes are human-curated; the ledger
    entry is the live record for the current session.
    """
    payload = envelope.payload
    change_summary = payload.get("change") or envelope.context_summary
    new_constraints = payload.get("context_constraints", {})

    detail_lines = [
        f"# Boundary change declared (signal {envelope.signal_id})",
        "",
        f"**Participant:** `{envelope.origin}`",
        "",
        f"**Change:** {change_summary}",
        "",
    ]
    if new_constraints:
        detail_lines += [
            "**New context constraints:**",
            "",
            "```json",
            json.dumps(new_constraints, indent=2),
            "```",
            "",
        ]
    detail_lines += [
        f"**Confidence:** {envelope.confidence:.2f}",
        "",
        "Per fnd-participants.md, declarations are living. This boundary_change "
        "entry records the declared change for the current session. The static "
        "declaration file in `participants/declarations/` is NOT modified by the "
        "orchestrator — permanent changes to the participant's declaration are "
        "human-curated and require editing the JSON file directly.",
    ]

    entry = _signal_to_ledger_entry(
        envelope,
        entry_type="boundary_change",
        summary=(
            f"Participant `{envelope.origin}` declared a boundary change: {change_summary}"
        ),
        detail="\n".join(detail_lines),
        foundation_tag=["boundaries"],
        scope=payload.get("scope", "coordination"),
    )
    write_entry(entry, repo)
    console.print(
        Panel(
            f"[bold yellow]Boundary change[/] from `{envelope.origin}` "
            f"(signal {envelope.signal_id})\n\n{change_summary}\n\n"
            f"Recorded as boundary_change entry [bold]{entry.entry_id}[/]. "
            f"Static declaration file unchanged.",
            title="signal: boundary_change",
            border_style="yellow",
        )
    )
    return entry


def handle_error(envelope: SignalEnvelope, repo: Repo) -> LedgerEntry | None:
    """Handle an `error` signal — a participant flagging a concern.

    Per fnd-failure.md, foundation violations are recorded as failure
    entries with a foundation_tag identifying which foundation is under
    strain. If the error signal cites a foundation in its payload, this
    handler writes a failure entry. Otherwise it surfaces the error to
    the human without a ledger entry (they decide whether to escalate).
    """
    payload = envelope.payload
    cited_foundations = payload.get("foundations") or payload.get("foundation_tag") or []
    description = payload.get("description") or envelope.context_summary

    if cited_foundations:
        entry = _signal_to_ledger_entry(
            envelope,
            entry_type="failure",
            summary=(
                f"Participant `{envelope.origin}` flagged a foundation concern: {description}. "
                f"Foundations cited: {', '.join(cited_foundations)}."
            ),
            detail=(
                f"# Foundation concern flagged via error signal {envelope.signal_id}\n\n"
                f"**Reporter:** `{envelope.origin}`\n\n"
                f"**Foundations cited:** {', '.join(cited_foundations)}\n\n"
                f"**Description:**\n\n{description}\n\n"
                f"**Reporter confidence:** {envelope.confidence:.2f}\n\n"
                f"**Lineage:** {envelope.lineage}\n\n"
                f"Per fnd-failure.md, this is recorded as a failure entry. The "
                f"coordination should consider entering the repair cycle — run "
                f"`python orchestrator.py repair --failure-entry {next_entry_id()}` "
                f"once the concern is diagnosed."
            ),
            foundation_tag=list(cited_foundations),
            scope=payload.get("scope", "coordination"),
        )
        write_entry(entry, repo)
        console.print(
            Panel(
                f"[bold red]Foundation concern[/] from `{envelope.origin}` "
                f"(signal {envelope.signal_id})\n\n{description}\n\n"
                f"Foundations cited: {', '.join(cited_foundations)}\n\n"
                f"Recorded as failure entry [bold]{entry.entry_id}[/]. "
                f"Consider running the repair cycle.",
                title="signal: error (foundation concern)",
                border_style="red",
            )
        )
        return entry

    console.print(
        Panel(
            f"[bold red]Error[/] from `{envelope.origin}` (signal {envelope.signal_id})\n\n"
            f"{description}\n\n"
            f"[dim]No foundation cited — surfaced for human review without a ledger entry.[/]",
            title="signal: error",
            border_style="red",
        )
    )
    return None


def handle_state_update(envelope: SignalEnvelope, repo: Repo) -> LedgerEntry | None:
    """Handle a `state_update` signal from a participant.

    A state_update conveys progress or state on a scope. Per fnd-ledger.md
    Write Protocol, participants propose entries via state_update signals.

    If `payload.proposed_entry` is a full ledger entry dict, validate and
    write it directly. Otherwise, synthesize an `attempt` entry from the
    payload's `scope` and `state` fields. Falls back to console-only if
    the payload doesn't carry enough structure for a ledger entry.
    """
    payload = envelope.payload or {}

    # Case 1: full proposed entry
    if "proposed_entry" in payload and isinstance(payload["proposed_entry"], dict):
        raw = payload["proposed_entry"]
        raw.setdefault("entry_id", "AUTO")
        raw.setdefault("timestamp", "AUTO")
        raw.setdefault("author", envelope.origin)
        try:
            entry = finalize_entry(raw, envelope.origin, raw.get("scope", "unknown"))
        except (ValidationError, ValueError) as e:
            console.print(
                f"[yellow]state_update from {envelope.origin}: proposed entry "
                f"failed validation: {e}[/]"
            )
            return None
        path = write_entry(entry, repo)
        console.print(
            Panel(
                f"[bold]State update → ledger entry[/]\n"
                f"[bold]From:[/] `{envelope.origin}` · [bold]Entry:[/] {entry.entry_id}\n"
                f"[bold]Type:[/] {entry.type} · [bold]Scope:[/] `{entry.scope}`\n\n"
                f"{entry.summary}",
                title=f"signal: state_update → {path.name}",
                border_style="green",
            )
        )
        return entry

    # Case 2: scope + state fields → synthesize an attempt entry
    scope = payload.get("scope")
    state = payload.get("state")
    if scope and state:
        entry = LedgerEntry(
            entry_id=next_entry_id(),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            author=envelope.origin,
            type="attempt",
            scope=scope,
            prior_entries=[],
            summary=f"State update from {envelope.origin}: {state}",
            detail=(
                f"# State update via signal\n\n"
                f"**Signal id:** {envelope.signal_id}\n\n"
                f"**State:** {state}\n\n"
                f"**Context:** {envelope.context_summary}\n\n"
                f"**Full payload:** {json.dumps(payload, indent=2)}"
            ),
            confidence=envelope.confidence,
            foundation_tag=["signal"],
        )
        write_entry(entry, repo)
        console.print(
            Panel(
                f"[bold]State update → attempt entry[/]\n"
                f"[bold]From:[/] `{envelope.origin}` · [bold]Scope:[/] `{scope}`\n\n"
                f"{state}",
                title=f"signal: state_update → {entry.entry_id}",
                border_style="green",
            )
        )
        return entry

    # Case 3: unstructured — surface to console only
    console.print(
        Panel(
            f"[bold]State update (unstructured)[/]\n"
            f"[bold]From:[/] `{envelope.origin}` → `{envelope.destination}`\n\n"
            f"{envelope.context_summary}\n\n"
            f"[dim]Payload:[/] {json.dumps(payload, indent=2)}\n\n"
            f"[dim]No `proposed_entry` or `scope`+`state` in payload — "
            f"archived for human review.[/]",
            title=f"signal: state_update",
            border_style="blue",
        )
    )
    return None


def handle_acknowledgment(envelope: SignalEnvelope, repo: Repo) -> LedgerEntry | None:
    """Handle an `acknowledgment` signal.

    An acknowledgment confirms receipt of a prior signal or task assignment.
    Per fnd-preamble.md, payload.response is one of: `accept`,
    `accept-with-conditions`, or `refuse-with-reason`.

    - accept / accept-with-conditions → write an `attempt` entry
      (per fnd-participants.md, acceptance is recorded as an attempt entry).
    - refuse-with-reason → write a `decision` entry recording the refusal
      so other participants can see the scope is available.
    - no response field → surface to console only (backward compat).
    """
    payload = envelope.payload or {}
    response = payload.get("response", "")
    scope = payload.get("scope", "unknown")
    reason = payload.get("reason", "")

    # Validate the acknowledged signal exists in archive
    ack_signal_ids = envelope.lineage
    for sid in ack_signal_ids:
        if not (SIGNAL_ARCHIVE / f"{sid}.json").exists():
            console.print(
                f"[yellow]acknowledgment {envelope.signal_id} references "
                f"signal {sid} not found in archive[/]"
            )

    if response in ("accept", "accept-with-conditions"):
        conditions = f" Conditions: {payload.get('conditions', 'none stated')}" if response == "accept-with-conditions" else ""
        entry = LedgerEntry(
            entry_id=next_entry_id(),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            author=envelope.origin,
            type="attempt",
            scope=scope,
            prior_entries=ack_signal_ids,
            summary=(
                f"{envelope.origin} accepted task on `{scope}`.{conditions}"
            ),
            detail=(
                f"# Task acceptance via acknowledgment signal\n\n"
                f"**Signal id:** {envelope.signal_id}\n\n"
                f"**Response:** {response}\n\n"
                f"**Acknowledged signals:** {', '.join(ack_signal_ids) or '—'}\n\n"
                f"**Context:** {envelope.context_summary}"
                + (f"\n\n**Conditions:** {payload.get('conditions', '')}" if response == "accept-with-conditions" else "")
            ),
            confidence=envelope.confidence,
            foundation_tag=["choice"],
        )
        write_entry(entry, repo)
        console.print(
            Panel(
                f"[bold green]Task accepted[/] by `{envelope.origin}` on `{scope}`"
                + (f"\n[dim]Conditions: {payload.get('conditions', '')}[/]" if response == "accept-with-conditions" else ""),
                title=f"signal: acknowledgment → attempt {entry.entry_id}",
                border_style="green",
            )
        )
        return entry

    if response == "refuse-with-reason":
        entry = LedgerEntry(
            entry_id=next_entry_id(),
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            author=envelope.origin,
            type="decision",
            scope=scope,
            prior_entries=ack_signal_ids,
            summary=(
                f"{envelope.origin} refused task on `{scope}`: {reason}"
            ),
            detail=(
                f"# Task refusal via acknowledgment signal\n\n"
                f"**Signal id:** {envelope.signal_id}\n\n"
                f"**Response:** refuse-with-reason\n\n"
                f"**Reason:** {reason}\n\n"
                f"**Acknowledged signals:** {', '.join(ack_signal_ids) or '—'}\n\n"
                f"**Context:** {envelope.context_summary}\n\n"
                f"Per fnd-participants.md → Refuse, the scope remains available "
                f"for other participants to pick up."
            ),
            confidence=envelope.confidence,
            foundation_tag=["choice", "boundaries"],
        )
        write_entry(entry, repo)
        console.print(
            Panel(
                f"[bold yellow]Task refused[/] by `{envelope.origin}` on `{scope}`\n"
                f"[dim]Reason: {reason}[/]",
                title=f"signal: acknowledgment → decision {entry.entry_id}",
                border_style="yellow",
            )
        )
        return entry

    # Unstructured acknowledgment — surface only
    console.print(
        Panel(
            f"[bold]Acknowledgment received[/]\n"
            f"[bold]From:[/] `{envelope.origin}` → `{envelope.destination}`\n"
            f"[bold]Acknowledged signals:[/] {', '.join(ack_signal_ids) or '—'}\n\n"
            f"{envelope.context_summary}\n\n"
            f"[dim]Payload:[/] {json.dumps(payload, indent=2)}",
            title=f"signal: acknowledgment",
            border_style="blue",
        )
    )
    return None


def handle_default(envelope: SignalEnvelope, repo: Repo) -> LedgerEntry | None:
    """Default handler for signal types without a specialized handler.

    Currently only `handoff` signals fall through to this handler (handoff
    signals coming FROM other participants — the orchestrator's own outgoing
    handoffs go to archive directly via write_outgoing_handoff). Surfaces
    the signal to the console and archives it. No ledger entry.
    """
    console.print(
        Panel(
            f"[bold]Signal received:[/] {envelope.type}\n"
            f"[bold]From:[/] `{envelope.origin}` → [bold]To:[/] `{envelope.destination}`\n"
            f"[bold]Signal id:[/] {envelope.signal_id}\n\n"
            f"{envelope.context_summary}\n\n"
            f"[dim]Payload:[/] {json.dumps(envelope.payload, indent=2)}\n\n"
            f"[dim]Archived for human review.[/]",
            title=f"signal: {envelope.type}",
            border_style="blue",
        )
    )
    return None


SIGNAL_HANDLERS = {
    "query": handle_query,
    "boundary_change": handle_boundary_change,
    "error": handle_error,
    "handoff": handle_default,
    "state_update": handle_state_update,
    "acknowledgment": handle_acknowledgment,
}


def validate_signal_lineage(envelope: SignalEnvelope) -> list[str]:
    """Check that every signal_id in `envelope.lineage` exists in archive.

    Returns a list of signal_ids that are referenced but not found. An empty
    list means all lineage references are valid. Per fnd-signal.md, lineage
    traceability is how participants verify the provenance of the signals
    they act on.

    Missing references are degraded signal, not fatal — the signal is still
    processed, but the warning alerts the human that the lineage chain has
    a gap.
    """
    _ensure_signal_dirs()
    missing: list[str] = []
    for sid in envelope.lineage:
        archive_path = SIGNAL_ARCHIVE / f"{sid}.json"
        if not archive_path.exists():
            missing.append(sid)
    return missing


def process_signal(envelope: SignalEnvelope, repo: Repo | None) -> LedgerEntry | None:
    """Receive an out-of-band signal: dispatch by type, then archive.

    The signal is written to inbox first (so there's a trace if processing
    fails), dispatched to the per-type handler, then moved to archive.
    Returns the ledger entry the handler wrote, if any.
    """
    # Lineage validation: warn on missing references, don't block.
    missing = validate_signal_lineage(envelope)
    if missing:
        console.print(
            f"[yellow]lineage warning:[/] signal {envelope.signal_id} references "
            f"{len(missing)} signal(s) not found in archive: {missing}. "
            f"The signal will still be processed, but the lineage chain has a gap."
        )

    write_signal_to_inbox(envelope)
    handler = SIGNAL_HANDLERS.get(envelope.type, handle_default)
    try:
        entry = handler(envelope, repo) if repo is not None else None
    except Exception as e:  # noqa: BLE001
        console.print(f"[red]signal handler error:[/] {e}")
        entry = None
    archive_signal(envelope, repo)
    return entry


def process_signals_from_response(
    text: str,
    source_decl: dict,
    repo: Repo,
) -> list[SignalEnvelope]:
    """Pull any signal envelopes from an agent response and process each.

    Called from request_entry_with_retry AFTER the entry is parsed. Signals
    are processed regardless of whether the entry parsed successfully —
    valid signal is valid signal even if the entry surrounding it is broken.
    """
    objs = extract_all_json(text)
    signals: list[SignalEnvelope] = []
    for obj in objs:
        if classify_json_object(obj) != "signal":
            continue
        # Auto-fields
        if obj.get("signal_id") in (None, "AUTO"):
            obj["signal_id"] = _next_signal_id()
        obj.setdefault("origin", source_decl["identifier"])
        obj.setdefault("destination", "orchestrator")
        obj.setdefault("timestamp", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        obj.setdefault("payload", {})
        obj.setdefault("lineage", [])
        try:
            envelope = SignalEnvelope(**obj)
        except (ValidationError, ValueError) as e:
            console.print(
                f"[yellow]warning: invalid signal envelope from {source_decl['identifier']}: {e}[/]"
            )
            continue
        # Author check (mirror of the entry author check)
        if envelope.origin != source_decl["identifier"]:
            console.print(
                f"[yellow]warning: signal {envelope.signal_id} claims origin "
                f"{envelope.origin} but came from {source_decl['identifier']}; "
                f"recording as-is[/]"
            )
        signals.append(envelope)
        process_signal(envelope, repo)
    return signals


# ---------- Ledger writes ----------

def write_entry(entry: LedgerEntry, repo: Repo | None) -> Path:
    """Persist a ledger entry. Git commit is optional — happens only when
    a repo is available. The file write itself is unconditional and is
    what gives the ledger its durability."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{entry.entry_id}-{entry.type}-{entry.author}.json"
    out = LEDGER_DIR / fname
    out.write_text(entry.model_dump_json(indent=2, exclude_none=True) + "\n", encoding="utf-8")
    if repo is not None:
        try:
            repo.index.add([str(out.relative_to(Path(repo.working_tree_dir)))])
            repo.index.commit(
                f"ledger: {entry.entry_id} {entry.type} from {entry.author} on {entry.scope}"
            )
        except Exception as e:  # noqa: BLE001 — git is optional
            console.print(f"[dim yellow]git commit skipped: {e}[/]")
    return out


def write_convergence_decision(
    repo: Repo,
    scope_path: str,
    participants: list[dict],
    conflict_protocol: str,
    intention: str,
    role_holder: str,
) -> LedgerEntry:
    """Record the convergence at the start of a multi-agent review.

    Per fnd-participants.md → Converge: a `decision` entry identifying the
    converging participants, the shared scope, and the conflict protocol.
    Authored by the orchestrator role-holder, not by a fictional
    "orchestrator" identity.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="decision",
        scope=scope_path,
        prior_entries=[],
        summary=(
            f"Convergence declared on {scope_path} between "
            f"{', '.join(p['identifier'] for p in participants)}. "
            f"Conflict protocol: {conflict_protocol}."
        ),
        detail=(
            f"# Convergence\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Intention:** {intention}\n\n"
            f"**Participants:**\n"
            + "\n".join(f"- `{p['identifier']}` (steward: {p.get('steward','?')})" for p in participants)
            + f"\n\n**Conflict protocol:** {conflict_protocol}\n\n"
            "Per fnd-participants.md, each participant gains shared ownership of "
            "this scope on accepting the review task. Incompatible verdicts on "
            "completion entries trigger the Conflict circuit breaker (see "
            "fnd-failure.md) and enter the repair cycle (fnd-repair.md)."
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


def write_conflict_failure(
    repo: Repo,
    scope_path: str,
    completion_entries: list[LedgerEntry],
    convergence_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Write a failure entry recording that the Conflict breaker fired.

    Authored by the orchestrator role-holder, who observed the breaker fire
    on tasks they routed.
    """
    failure_id = next_entry_id()  # nothing has been written yet, so this is OUR id
    verdict_lines = "\n".join(
        f"- `{e.author}` → **{e.verdict}** (confidence {e.confidence:.2f}) — entry {e.entry_id}"
        for e in completion_entries
    )
    entry = LedgerEntry(
        entry_id=failure_id,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[convergence_entry_id] + [e.entry_id for e in completion_entries],
        summary=(
            f"Conflict circuit breaker fired on {scope_path}: convergent reviewers "
            f"returned incompatible verdicts. Repair cycle required."
        ),
        detail=(
            f"# Conflict Detected\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Convergence entry:** {convergence_entry_id}\n\n"
            f"**Reviewer verdicts:**\n{verdict_lines}\n\n"
            "Per the convergence's declared conflict protocol, this failure "
            "enters the repair cycle. Run:\n\n"
            f"    python orchestrator.py repair --failure-entry {failure_id}\n\n"
            "An arbiter will load `fnd-failure.md` and `fnd-repair.md`, read the "
            "completion entries above, diagnose the disagreement, and propose a "
            "`repair` entry that links back to this failure."
        ),
        confidence=1.0,
        foundation_tag=["truth", "boundaries"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


# ---------- Capability-based routing ----------

# Maps file extensions to task types for infer_task_type(). The task type
# is matched against each participant's `preferred_tasks` list from their
# declaration. If no match is found, all active agents review the scope
# (backward compatible broadcast behavior).
_EXTENSION_TASK_TYPE: dict[str, str] = {
    ".py": "code_review",
    ".js": "code_review",
    ".ts": "code_review",
    ".go": "code_review",
    ".rs": "code_review",
    ".java": "code_review",
    ".c": "code_review",
    ".cpp": "code_review",
    ".rb": "code_review",
    ".sh": "code_review",
    ".md": "writing_review",
    ".txt": "writing_review",
    ".rst": "writing_review",
    ".json": "code_review",
    ".yaml": "code_review",
    ".yml": "code_review",
    ".toml": "code_review",
}


def infer_task_type(scope_path: str) -> str | None:
    """Map a scope path's file extension to a task type.

    Returns None if the extension isn't recognized. Callers fall back to
    broadcasting to all active agents when the task type is unknown.
    """
    ext = Path(scope_path).suffix.lower()
    return _EXTENSION_TASK_TYPE.get(ext)


def route_participants(
    declarations: list[dict],
    task_type: str | None,
) -> list[dict]:
    """Select agents whose `preferred_tasks` match the task type.

    Per fnd-field.md: 'Routing considers declaration match, boundaries,
    resource state, cost, and complementarity.' This implementation covers
    the first criterion — declaration match via preferred_tasks. Resource
    state is checked separately by the Resource breaker after review.

    If task_type is None or no agents' preferred_tasks contain it, falls
    back to all active agents (backward compatible). This ensures that
    introducing capability routing never reduces the set of participants
    below zero.

    Agents are sorted by their capability_envelope score for the task type
    (descending), with ties broken by declaration order. This is a mild
    preference, not a hard filter — per fnd-field.md, 'when multiple
    participants are materially fit, prefer the routing choice that broadens
    stewardship, lineage, or failure profile.'
    """
    active = [
        d for d in declarations
        if d.get("participation_mode") == "active" and d.get("litellm_model")
    ]

    if not task_type:
        return active

    matched = [
        d for d in active
        if task_type in (d.get("preferred_tasks") or [])
    ]

    if not matched:
        return active

    # Sort by capability score for this task type (descending)
    def score(d: dict) -> float:
        return (d.get("capability_envelope") or {}).get(task_type, 0.0)

    matched.sort(key=score, reverse=True)
    return matched


# ---------- Review loop ----------

def run_review(scope_rel: str, task_type: str | None = None) -> int:
    config = load_config()
    declarations = load_declarations()
    foundations_text = load_foundations(config.get("foundations_loaded_by_default", []))
    intention = config.get("intention", "")
    convergence_cfg = config.get("convergence", {})
    conflict_protocol = convergence_cfg.get("default_protocol", "escalate_to_repair")

    scope_abs = ROOT / scope_rel
    if not scope_abs.exists():
        console.print(f"[red]error:[/] scope file not found: {scope_rel}")
        return 2
    scope_content = scope_abs.read_text(encoding="utf-8")

    # ---- Gate: routing requires that someone holds the orchestrator role ----
    role_holder = current_orchestrator_for_scope(scope_rel)

    # ---- Repetition breaker check (before any work) ----
    # Independent of role-holder presence: even with a role-holder, the
    # breaker fires if there are 3+ unresolved failures on the scope. This
    # prevents the role-holder from grinding through repeated identical
    # attempts.
    existing_entries = entries_for_scope(scope_rel)
    if repetition_breaker_should_fire(existing_entries):
        unresolved = unresolved_failures_for_scope(existing_entries)
        if role_holder is None:
            console.print(
                Panel(
                    f"[bold red]REPETITION CIRCUIT BREAKER FIRED on {scope_rel}.[/]\n\n"
                    f"{len(unresolved)} unrepaired failures accumulated. Even taking "
                    f"the orchestrator role would not unblock this — the breaker "
                    f"requires repair first.\n\n"
                    f"Run repair on each:\n"
                    + "\n".join(
                        f"  [cyan]python orchestrator.py repair --failure-entry {e.entry_id}[/]"
                        for e in unresolved
                    ),
                    title="repetition breaker",
                    border_style="red",
                )
            )
            return 3
        # With a role-holder, write the breaker failure entry attributed to them
        repo = get_repo()
        rep_failure = write_repetition_failure(repo, scope_rel, unresolved, role_holder)
        console.print(
            Panel(
                f"[bold red]REPETITION CIRCUIT BREAKER FIRED on {scope_rel}.[/]\n\n"
                f"{len(unresolved)} unrepaired failures accumulated. Failure entry "
                f"[bold]{rep_failure.entry_id}[/] written. Routing on this scope "
                f"is now refused until repair is complete.\n\n"
                f"Run repair on each unresolved failure listed in entry "
                f"{rep_failure.entry_id}'s detail.",
                title="repetition breaker",
                border_style="red",
            )
        )
        return 3

    if role_holder is None:
        console.print(
            Panel(
                f"[bold red]Cannot route review on {scope_rel}.[/]\n\n"
                f"No participant currently holds the orchestrator role for this "
                f"scope. Per fnd-field.md, routing is something the orchestrator "
                f"role-holder does — it is not a system feature. The script "
                f"orchestrator.py is the tool the role-holder uses, not the "
                f"orchestrator itself.\n\n"
                f"Either:\n"
                f"  1. [cyan]python orchestrator.py take-role --scope {scope_rel} "
                f"--as <participant>[/] then re-run review\n"
                f"  2. (future) Surface the scope as an open question via "
                f"`synthesize`-style emergent transition and let participants "
                f"self-select",
                title="no orchestrator",
                border_style="red",
            )
        )
        return 2

    repo = get_repo()

    # Capability-based routing: if a task_type was provided or inferred,
    # prefer agents whose preferred_tasks match. Falls back to all active
    # agents if no match or no task_type.
    effective_task_type = task_type or infer_task_type(scope_rel)
    active_agents = route_participants(declarations, effective_task_type)
    if not active_agents:
        console.print("[red]error:[/] no active agents with a litellm_model declared")
        return 2

    routing_note = ""
    if effective_task_type:
        all_active = [d for d in declarations if d.get("participation_mode") == "active" and d.get("litellm_model")]
        if len(active_agents) < len(all_active):
            routing_note = f"\n[bold]Routing:[/] capability-routed for task type `{effective_task_type}`"
        else:
            routing_note = f"\n[bold]Routing:[/] broadcast (no agents matched `{effective_task_type}` in preferred_tasks)"

    console.print(
        Panel.fit(
            f"[bold]Scope:[/] {scope_rel}\n"
            f"[bold]Intention:[/] {intention}\n"
            f"[bold]Role holder:[/] {role_holder}\n"
            f"[bold]Reviewers:[/] {', '.join(d['identifier'] for d in active_agents)}\n"
            f"[bold]Conflict protocol:[/] {conflict_protocol}"
            + routing_note,
            title="Coordination Review",
        )
    )

    convergence_entry = write_convergence_decision(
        repo, scope_rel, active_agents, conflict_protocol, intention, role_holder
    )
    console.print(
        f"[dim]wrote convergence decision {convergence_entry.entry_id}[/]"
    )

    completion_entries: list[LedgerEntry] = []
    results: list[dict[str, Any]] = []
    for decl in active_agents:
        author = decl["identifier"]
        model = decl["litellm_model"]
        co_reviewers = [d for d in active_agents if d["identifier"] != author]
        console.print(f"\n[cyan]→ requesting review from {author} ({model})…[/]")

        base_messages = build_messages(
            decl,
            foundations_text,
            intention,
            scope_rel,
            scope_content,
            co_reviewers,
            convergence_entry.entry_id,
        )

        entry, error = request_entry_with_retry(
            decl=decl,
            base_messages=base_messages,
            expected_types=("completion",),
            required_prior_entries=(convergence_entry.entry_id,),
            scope_path=scope_rel,
            repo=repo,
            from_participant=role_holder,
            handoff_task_type="review",
        )

        if entry is None:
            console.print(f"  [red]✗ {author} could not produce a valid entry:[/] {error}")
            failure = write_participant_failure(
                repo, scope_rel, decl, error or "unknown",
                convergence_entry.entry_id, role_holder,
            )
            results.append({
                "author": author,
                "entry_id": failure.entry_id,
                "type": failure.type,
                "verdict": None,
                "confidence": failure.confidence,
                "summary": failure.summary,
                "error": error,
            })
            continue

        path = write_entry(entry, repo)
        if entry.type == "completion":
            completion_entries.append(entry)
            console.print(
                f"  [green]✓[/] wrote {path.name} "
                f"(verdict={entry.verdict or '—'}, confidence={entry.confidence:.2f})"
            )
        elif entry.type == "failure":
            console.print(
                f"  [yellow]⊘[/] {author} refused: {entry.summary}"
            )
        if entry.confidence < config["circuit_breakers"]["confidence_floor"]:
            console.print(
                f"  [yellow]⚠ confidence breaker would fire (< "
                f"{config['circuit_breakers']['confidence_floor']})[/]"
            )
        results.append({
            "author": author,
            "entry_id": entry.entry_id,
            "type": entry.type,
            "verdict": entry.verdict,
            "confidence": entry.confidence,
            "summary": entry.summary,
            "path": str(path.relative_to(ROOT)),
            "tokens_in": None,  # usage tracking is now inside the retry helper; surface later
            "tokens_out": None,
            "error": None,
        })

    print_summary(results)

    # ---- Conflict circuit breaker ----
    conflict_fired = detect_verdict_conflict(completion_entries)
    if conflict_fired:
        failure = write_conflict_failure(
            repo, scope_rel, completion_entries, convergence_entry.entry_id, role_holder,
        )
        console.print(
            Panel(
                f"[bold red]CONFLICT CIRCUIT BREAKER FIRED[/]\n\n"
                f"Convergent reviewers on [bold]{scope_rel}[/] returned incompatible verdicts.\n"
                f"Failure entry: [bold]{failure.entry_id}[/]\n\n"
                f"Per the declared conflict protocol ([italic]{conflict_protocol}[/]),\n"
                f"the coordination must enter the repair cycle (fnd-repair.md).\n\n"
                f"Next: [bold cyan]python orchestrator.py repair --failure-entry {failure.entry_id} --arbiter <identifier>[/]",
                title="!!! repair cycle required !!!",
                border_style="red",
            )
        )
        return 3

    # ---- Resource circuit breaker ----
    resource_failure = check_resource_breaker(config, scope_rel, role_holder, repo)
    if resource_failure is not None:
        console.print(
            Panel(
                f"[bold red]RESOURCE CIRCUIT BREAKER FIRED[/]\n\n"
                f"{resource_failure.summary}\n\n"
                f"Failure entry: [bold]{resource_failure.entry_id}[/]\n\n"
                f"Per fnd-failure.md, disproportionate resource usage is a "
                f"Balance concern. Consider rebalancing the participant roster "
                f"or adjusting the scope.",
                title="!!! resource breaker !!!",
                border_style="red",
            )
        )
        return 3

    # Per-participant ceiling check
    for decl in active_agents:
        ceiling_failure = check_resource_ceiling(
            decl["identifier"], declarations, scope_rel, role_holder, repo,
        )
        if ceiling_failure is not None:
            console.print(
                Panel(
                    f"[bold red]RESOURCE CEILING BREACHED[/]\n\n"
                    f"{ceiling_failure.summary}\n\n"
                    f"Failure entry: [bold]{ceiling_failure.entry_id}[/]",
                    title="!!! resource ceiling !!!",
                    border_style="red",
                )
            )
            return 3

    if all(r.get("error") is None for r in results):
        return 0
    return 1


def detect_verdict_conflict(entries: list[LedgerEntry]) -> bool:
    """The Conflict breaker, in its first form.

    Two or more completion entries on the same scope with different verdicts
    is treated as incompatible state proposals (per fnd-ledger.md write
    protocol). `no_judgment` and `None` do not participate in the comparison
    — they are abstentions, not positions.
    """
    verdicts = {e.verdict for e in entries if e.verdict and e.verdict != "no_judgment"}
    return len(verdicts) >= 2


# ---------- Repair cycle ----------

REPAIR_SYSTEM_TEMPLATE = """You are acting as the ARBITER in a repair cycle
for a multi-AI-agent coordination governed by the Foundations of Multi-AI-Agent
Coordination v0.4. A circuit breaker has fired and the coordination is paused.

Your declaration in this coordination is:
{declaration_json}

Coordination intention:
{intention}

The repair cycle is defined in the foundations loaded below. Read fnd-failure.md
and fnd-repair.md as your operating contract for this task.

{foundations}

You will be given:
  1. A `failure` ledger entry recording which breaker fired and why
  2. The completion entries the convergent reviewers wrote, which contain
     their incompatible verdicts and full reasoning

Your job is to perform the repair cycle (Pause → Diagnose → Surface → Resolve
→ Verify → Record), then return ONE JSON ledger entry of type `repair`. No
prose before or after the JSON.

The `repair` entry MUST:
  - have type = "repair"
  - have prior_entries that include the failure entry's id and the entry_ids
    of every completion entry you considered
  - have a `summary` that names the diagnosed root cause in 1-3 sentences
  - have a `detail` field structured with: ## Diagnosis, ## Resolution,
    ## Verification (or why rerun is unsafe), ## Lessons. Markdown is fine.
  - have a `confidence` reflecting how sure you are the resolution will hold
  - have foundation_tag listing which foundations are relevant (truth,
    boundaries, balance, etc.)
  - leave `entry_id` and `timestamp` as the literal string "AUTO"
  - set `verdict` to one of: "approve", "approve_with_conditions", "reject",
    "escalate" — your judgment of the underlying scope artifact AFTER the
    repair, since the repair must yield a coherent decision the coordination
    can act on. Use "escalate" if even with diagnosis you cannot pick.

Per fnd-repair.md: good faith first, full signal, accountability without
annihilation, demonstrated change, restoration. The repair is not a verdict
on the reviewers — it is a re-establishment of coherent shared truth.

{signal_docs}
"""

REPAIR_USER_TEMPLATE = """## Ledger Summary (orienting context per fnd-ledger.md)

The summary below is the compressed ledger view per fnd-ledger.md → Read
Protocol → Ledger Summary. Failure, repair, intention_shift, and
boundary_change entries are shown in full because they carry the highest
signal density for diagnosing what went wrong. Other entries are compressed.
Read this to orient before diagnosing the specific failure below.

{ledger_summary}

---

## Failure entry (the breaker that fired)

```json
{failure_json}
```

## Completion entries from the convergent reviewers

{completion_blocks}

Diagnose, resolve, and return one `repair` JSON entry.
"""


def load_entry(entry_id: str) -> LedgerEntry:
    matches = list(LEDGER_DIR.glob(f"{entry_id}-*.json"))
    if not matches:
        raise FileNotFoundError(f"no ledger entry with id {entry_id}")
    return LedgerEntry(**json.loads(matches[0].read_text(encoding="utf-8")))


VERIFICATION_SYSTEM_TEMPLATE = """You are a participant in a multi-AI-agent coordination.
You are performing a VERIFICATION RERUN — a limited re-review of a scope
artifact under conditions that have been modified by a repair entry.

Your declaration:
{declaration_json}

Coordination intention:
{intention}

Foundations:
{foundations}

The repair entry below resolved a conflict or failure on this scope.
Your task is to re-review the scope artifact under the resolved conditions
and confirm whether the repair holds. Produce a `completion` entry with:
  - verdict: one of approve, approve_with_conditions, reject, escalate
  - prior_entries MUST include "{repair_entry_id}"
  - summary: whether the scope is now fit under the repaired conditions
  - detail: your full reasoning

If the same issue that triggered the original failure recurs, that is
strong signal that the repair did not hold. Return your honest assessment.

{signal_docs}
"""

VERIFICATION_USER_TEMPLATE = """## Repair entry (the resolution being verified)

```json
{repair_json}
```

## Original failure entry

```json
{failure_json}
```

## Scope artifact

```{lang}
{scope_content}
```

Re-review the scope under the repaired conditions. Return one JSON ledger entry.
"""


def run_verification_rerun(
    repair: LedgerEntry,
    failure: LedgerEntry,
    original_completions: list[LedgerEntry],
    repo: Repo | None,
    config: dict,
    role_holder: str,
) -> int:
    """Per fnd-repair.md: 'Verification must either include a limited rerun
    of the failed work under the resolved conditions, or explicitly record
    why rerun is impossible or unsafe.'

    This runs the original failing participants (extracted from the
    completions linked to the failure) through a verification review,
    with the repair entry as additional context.
    """
    declarations = load_declarations()
    intention = config.get("intention", "")
    foundations_text = load_foundations([
        "fnd-preamble.md", "fnd-failure.md", "fnd-repair.md", "fnd-ledger.md"
    ])

    # Determine who to re-verify with: the original participants who produced
    # the conflicting completions
    original_authors = {e.author for e in original_completions}
    verify_agents = [
        d for d in declarations
        if d["identifier"] in original_authors and d.get("litellm_model")
    ]

    if not verify_agents:
        console.print(
            "[yellow]No original reviewers available for verification rerun "
            "(they may lack litellm_model). Recording verification as skipped.[/]"
        )
        return 0

    scope_abs = ROOT / failure.scope
    if not scope_abs.exists():
        console.print(f"[red]error:[/] scope file not found for verification: {failure.scope}")
        return 2
    scope_content = scope_abs.read_text(encoding="utf-8")
    lang = Path(failure.scope).suffix.lstrip(".") or "text"

    any_failed = False
    for decl in verify_agents:
        author = decl["identifier"]
        console.print(f"\n[cyan]→ verification rerun for {author}…[/]")

        system = VERIFICATION_SYSTEM_TEMPLATE.format(
            declaration_json=json.dumps(decl, indent=2),
            intention=intention,
            foundations=foundations_text,
            repair_entry_id=repair.entry_id,
            signal_docs=SIGNAL_ENVELOPE_DOCS,
        )
        user = VERIFICATION_USER_TEMPLATE.format(
            repair_json=repair.model_dump_json(indent=2, exclude_none=True),
            failure_json=failure.model_dump_json(indent=2, exclude_none=True),
            lang=lang,
            scope_content=scope_content,
        )
        base_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        entry, error = request_entry_with_retry(
            decl=decl,
            base_messages=base_messages,
            expected_types=("completion",),
            required_prior_entries=(repair.entry_id,),
            scope_path=failure.scope,
            repo=repo,
            from_participant=role_holder,
            handoff_task_type="verification_rerun",
        )

        if entry is None:
            console.print(f"  [red]✗ {author} could not produce a verification entry:[/] {error}")
            any_failed = True
            continue

        write_entry(entry, repo)
        console.print(
            f"  [green]✓[/] {author} verification: verdict={entry.verdict or '—'}, "
            f"confidence={entry.confidence:.2f}"
        )

        if entry.verdict in ("reject", "escalate"):
            console.print(
                f"  [yellow]⚠ {author}'s verification suggests the repair may not hold.[/]"
            )
            any_failed = True

    return 1 if any_failed else 0


def run_repair(failure_entry_id: str, arbiter_id: str | None, verify: bool = False) -> int:
    config = load_config()
    declarations = load_declarations()
    intention = config.get("intention", "")

    # Load failure entry and the linked completions.
    try:
        failure = load_entry(failure_entry_id)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    if failure.type != "failure":
        console.print(f"[red]error:[/] entry {failure_entry_id} is type={failure.type}, not failure")
        return 2

    # ---- Gate: routing the repair to an arbiter requires the orchestrator role ----
    role_holder = current_orchestrator_for_scope(failure.scope)
    if role_holder is None:
        console.print(
            Panel(
                f"[bold red]Cannot route repair on {failure.scope}.[/]\n\n"
                f"No participant currently holds the orchestrator role for this "
                f"scope. Choosing an arbiter and routing the repair task to them "
                f"is an orchestration decision — it cannot be done without a "
                f"role-holder.\n\n"
                f"Take the role first:\n"
                f"  [cyan]python orchestrator.py take-role --scope {failure.scope} "
                f"--as <participant>[/]",
                title="no orchestrator",
                border_style="red",
            )
        )
        return 2

    completion_ids = [
        eid for eid in failure.prior_entries
        if not eid.startswith(failure.entry_id)
    ]
    completions: list[LedgerEntry] = []
    for eid in completion_ids:
        try:
            e = load_entry(eid)
            if e.type == "completion":
                completions.append(e)
        except FileNotFoundError:
            console.print(f"[yellow]warning:[/] linked entry {eid} not found, skipping")

    if not completions:
        console.print("[red]error:[/] no completion entries linked from failure; nothing to arbitrate")
        return 2

    # Pick the arbiter.
    arbiter_id = arbiter_id or config.get("convergence", {}).get("arbiter") or "human-lead"
    arbiter = next((d for d in declarations if d["identifier"] == arbiter_id), None)
    if arbiter is None:
        console.print(f"[red]error:[/] no declaration found for arbiter '{arbiter_id}'")
        return 2
    if not arbiter.get("litellm_model"):
        console.print(
            Panel(
                f"[bold]Arbiter[/] [italic]{arbiter_id}[/] has no litellm_model — cannot run automated repair.\n\n"
                f"This is the expected case for [bold]human-lead[/]. The repair cycle is now [bold]your[/] turn:\n"
                f"  1. Read fnd-repair.md\n"
                f"  2. Read failure entry [bold]{failure.entry_id}[/] and its linked completions\n"
                f"  3. Hand-write a `repair` ledger entry that links back via prior_entries\n\n"
                f"Or rerun with [cyan]--arbiter <agent>[/] naming an agent that has a litellm_model.",
                title="repair: human turn",
                border_style="yellow",
            )
        )
        return 0

    # Load the broader foundations needed for repair.
    repair_foundations = load_foundations(
        ["fnd-preamble.md", "fnd-failure.md", "fnd-repair.md", "fnd-ledger.md"]
    )

    completion_blocks = "\n\n".join(
        f"### entry {e.entry_id} — `{e.author}` (verdict: {e.verdict}, confidence: {e.confidence:.2f})\n\n"
        f"```json\n{e.model_dump_json(indent=2, exclude_none=True)}\n```"
        for e in completions
    )

    system = REPAIR_SYSTEM_TEMPLATE.format(
        declaration_json=json.dumps(arbiter, indent=2),
        intention=intention,
        foundations=repair_foundations,
        signal_docs=SIGNAL_ENVELOPE_DOCS,
    )
    user = REPAIR_USER_TEMPLATE.format(
        ledger_summary=summarize_ledger(active_scope=failure.scope),
        failure_json=failure.model_dump_json(indent=2, exclude_none=True),
        completion_blocks=completion_blocks,
    )

    console.print(
        Panel.fit(
            f"[bold]Arbiter:[/] {arbiter_id} ({arbiter['litellm_model']})\n"
            f"[bold]Failure:[/] {failure.entry_id} on {failure.scope}\n"
            f"[bold]Considering:[/] {', '.join(e.entry_id for e in completions)}",
            title="Repair Cycle",
        )
    )

    repo = get_repo()
    base_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    required_priors = (failure.entry_id,) + tuple(e.entry_id for e in completions)
    repair, error = request_entry_with_retry(
        decl=arbiter,
        base_messages=base_messages,
        expected_types=("repair",),
        required_prior_entries=required_priors,
        scope_path=failure.scope,
        repo=repo,
        from_participant=role_holder,
        handoff_task_type="repair",
    )
    if repair is None:
        console.print(f"[red]repair failed:[/] {error}")
        write_participant_failure(
            repo, failure.scope, arbiter, error or "unknown",
            failure.entry_id, role_holder,
        )
        return 1
    if repair.type == "failure":
        console.print(
            Panel(
                f"[yellow]Arbiter refused to produce a repair entry.[/]\n\n"
                f"{repair.summary}\n\n"
                f"Per fnd-repair.md, the arbiter's refusal is itself a repair-cycle "
                f"signal. The cycle is not closed — try a different arbiter, or "
                f"escalate to human-lead.",
                title=f"refusal {repair.entry_id}",
                border_style="yellow",
            )
        )
        write_entry(repair, repo)
        return 1
    path = write_entry(repair, repo)
    console.print(
        Panel(
            f"[bold]Repair entry written:[/] {path.name}\n"
            f"[bold]Verdict:[/] {repair.verdict or '—'}  ·  [bold]Confidence:[/] {repair.confidence:.2f}\n\n"
            f"{repair.summary}",
            title=f"repair {repair.entry_id}",
            border_style="green",
        )
    )

    # ---- Verification rerun (opt-in via --verify) ----
    if verify:
        console.print("\n[bold cyan]Running verification rerun…[/]")
        verify_result = run_verification_rerun(
            repair, failure, completions, repo, config, role_holder,
        )
        if verify_result != 0:
            console.print(
                "[yellow]Verification rerun did not fully pass. "
                "Check the new ledger entries for details.[/]"
            )
            return verify_result
        console.print("[green]Verification rerun completed successfully.[/]")

    return 0


# ---------- Synthesis as Emergent Mode transition ----------
#
# Synthesis is NOT a single-arbiter operation. The orchestrator transitions
# the field from Orchestrated to Emergent (per fnd-field.md), records the
# question being explored as an `intention_shift` entry, and surfaces the
# question to every active participant. Each participant SELF-SELECTS:
# they may propose a synthesis decision OR refuse with reason. The aggregate
# of proposals IS the synthesis — convergence among them is strong signal,
# divergence is also legitimate signal, and the human reads both.
#
# No participant is elevated. There is no "synthesizer" role.

SELF_SELECT_SYNTHESIS_SYSTEM_TEMPLATE = """You are a participant in a
multi-AI-agent coordination governed by the Foundations of Multi-AI-Agent
Coordination v0.4. The coordination has just transitioned from Orchestrated
to **Emergent Mode** for the scope below. In Emergent Mode (see fnd-field.md)
participants SELF-SELECT — there is no central routing, and no participant
holds the orchestrator role for this question.

Your declaration in this coordination is:
{declaration_json}

Coordination intention (unchanged):
{intention}

Open question (recorded as intention_shift {intention_shift_id}):
{synthesis_question}

Mode transition entry: {transition_id}

Foundations loaded for this task:

{foundations}

You are being invited — alongside every other active participant — to
SELF-SELECT a response. You have three honest options:

  1. **Propose a synthesis decision.** Read the prior reviewers' completion
     entries and the original scope, and write a `decision` entry expressing
     your proposed unified position. Other participants are doing the same;
     yours is one voice in the aggregate, not a verdict over them.

  2. **Refuse with reason.** Return a `failure` entry whose `detail` explains
     why you decline to propose. Valid reasons include: you already
     participated as a reviewer and your synthesis would inherit your prior
     framing; your context is insufficient; you do not believe the question
     is well-formed; etc. Refusal is signal, not failure.

  3. **Refuse silently is NOT a third option.** Per fnd-signal.md, "silence
     is not acknowledgment." If you do not propose, you must refuse with
     reason so the ledger records why.

If you propose a `decision` entry, it MUST:
  - have type = "decision"
  - have prior_entries that include {required_priors_str} plus the entry_ids
    of every reviewer completion you actually drew on (this is YOUR claim
    about your own dependencies — link the ones you read)
  - have a `summary` (1-3 sentences) stating your proposed unified position
  - have a `detail` field structured as markdown with these sections:
      ## Reviewers Considered
      ## Convergent Findings        — what reviewers agreed on
      ## Divergent Emphases         — where they differed in detail (not verdict)
      ## My Proposed Position       — the position you are proposing
      ## Open Questions for human-lead
  - have a `confidence` reflecting how solid YOUR proposal is given the
    underlying material; do not inherit reviewer confidence wholesale
  - have foundation_tag listing the foundations you consider relevant to
    your proposal (this is your claim, not a fixed list)
  - set `verdict` to one of "approve", "approve_with_conditions", "reject",
    or "escalate" — your proposed verdict on the underlying scope artifact
  - leave `entry_id` and `timestamp` as the literal string "AUTO"

Do NOT introduce findings the reviewers did not raise. Synthesis compresses
and reconciles existing signal; it does not invent new signal. If you notice
something the reviewers missed, name it in "Open Questions for human-lead".

If your honest assessment is that another participant's perspective would
serve the question better than yours, refuse with that reason — that is a
legitimate Choice expression and contributes more signal than a thin
proposal made out of obligation.

{signal_docs}
"""

SELF_SELECT_SYNTHESIS_USER_TEMPLATE = """## Ledger Summary (orienting context per fnd-ledger.md)

The summary below is the compressed ledger view per fnd-ledger.md → Read
Protocol → Ledger Summary. Failure, repair, intention_shift, and
boundary_change entries are shown in full because they define current
operating conditions and prior conflicts. Decision/attempt/completion
entries on this synthesis scope are shown in summary form; entries on
other scopes are compressed to one line. Read this to orient before
forming your synthesis position below.

{ledger_summary}

---

## Convergence decision (the review that opened this scope)

```json
{convergence_json}
```

## Completion entries from the prior reviewers

{completion_blocks}

## Repair entries (if any earlier conflicts on this scope were resolved)

{repair_blocks}

## Original scope artifact (for grounding only — do not re-review)

```{lang}
{scope_content}
```

Self-select your response. Return one JSON entry — either a `decision`
proposing your synthesis position, or a `failure` declining with reason.
"""


def write_mode_transition_decision(
    repo: Repo,
    scope_path: str,
    from_mode: str,
    to_mode: str,
    reason: str,
    triggering_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Per fnd-field.md, mode transitions are ledger entries of type decision.
    Authored by whoever proposed the transition — typically the role-holder
    who is moving the scope into a new mode.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="decision",
        scope=scope_path,
        prior_entries=[triggering_entry_id],
        summary=(
            f"Field mode transition on {scope_path}: {from_mode} → {to_mode}. {reason}"
        ),
        detail=(
            f"# Mode transition: {from_mode} → {to_mode}\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Reason:** {reason}\n\n"
            f"**Triggering entry:** {triggering_entry_id}\n\n"
            f"Per fnd-field.md, this transition is recorded as a `decision` entry."
            + (
                f" In {to_mode} Mode, no participant holds the orchestrator role "
                f"for this scope; participants self-select tasks based on their "
                f"own assessment of where they can contribute."
                if to_mode == "emergent"
                else
                f" The scope returns to {to_mode} mode. No active orchestrated "
                f"workflows or open emergent questions remain. A participant may "
                f"take the orchestrator role to begin new work on this scope."
            )
            + f" The orchestrator stays available for infrastructure-mode functions "
            f"(validation, breaker monitoring) but does not route or assign."
        ),
        confidence=1.0,
        foundation_tag=["choice", "intention"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


def write_intention_shift(
    repo: Repo,
    scope_path: str,
    question: str,
    transition_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Per fnd-field.md: 'A transition to Emergent Mode should be accompanied
    by a clear statement of the question being explored, recorded in the
    ledger as an intention_shift entry.'
    Authored by the role-holder who proposed the question.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="intention_shift",
        scope=scope_path,
        prior_entries=[transition_entry_id],
        summary=f"Question opened for emergent exploration: {question}",
        detail=(
            f"# Intention shift: open question for emergent exploration\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Question:** {question}\n\n"
            f"**Mode transition:** {transition_entry_id}\n\n"
            f"This is not a change to the coordination's overall intention. It is "
            f"a refinement: a specific question being opened for self-selecting "
            f"participants in Emergent Mode. The original coordination intention "
            f"remains in force. Per fnd-field.md, all active participants in scope "
            f"should re-evaluate whether they can contribute to this question."
        ),
        confidence=1.0,
        foundation_tag=["intention"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


def entries_for_scope(scope_rel: str) -> list[LedgerEntry]:
    """All ledger entries whose `scope` field matches, in id order."""
    out: list[LedgerEntry] = []
    for p in sorted(LEDGER_DIR.glob("*.json")):
        try:
            entry = LedgerEntry(**json.loads(p.read_text(encoding="utf-8")))
        except (ValidationError, json.JSONDecodeError):
            continue
        if entry.scope == scope_rel:
            out.append(entry)
    return out


# Soft byte cap for the generated summary. Per fnd-ledger.md the threshold
# is "the smallest context window of any active participant" — we don't
# know that statically, so we pick a conservative default that fits in
# every modern model's context with room to spare. Exceeding it is signal,
# not failure: we still return the full summary, just print a warning.
LEDGER_SUMMARY_SOFT_LIMIT_BYTES = 50_000

# Entry types whose detail is preserved in full by `summarize_ledger`,
# per fnd-ledger.md → Read Protocol → Ledger Summary:
#   - failure / repair carry the highest signal density for preventing
#     repeated mistakes
#   - intention_shift / boundary_change define current operating conditions
_PRESERVE_IN_FULL = {"failure", "repair", "intention_shift", "boundary_change"}


def summarize_ledger(
    active_scope: str | None = None,
    soft_limit_bytes: int = LEDGER_SUMMARY_SOFT_LIMIT_BYTES,
) -> str:
    """Generate the compressed ledger view per fnd-ledger.md → Ledger Summary.

    Compression rules mirror the foundation spec verbatim:

      - `failure`, `repair`, `intention_shift`, and `boundary_change`
        entries are preserved in full (summary AND detail). The spec
        marks these as the highest-signal entries — failures and repairs
        prevent recursive failure loops, intention_shift and
        boundary_change define current operating conditions.

      - `decision`, `attempt`, and `completion` entries whose `scope`
        matches `active_scope` are included with their `summary` field
        only (detail omitted).

      - All other `decision` / `attempt` / `completion` entries are
        compressed to a single line: id, author, type, scope, summary.

      - If `active_scope` is None, every decision/attempt/completion gets
        the one-line treatment. The full-preservation types are unaffected.

    The output is markdown text suitable for terminal display AND for
    embedding in LLM system or user prompts.

    A Balance warning is printed to stderr (via `console`) if the resulting
    text exceeds `soft_limit_bytes`. Per fnd-ledger.md: "If it is not
    [small enough], the coordination has grown beyond what its current
    participants can hold, and that itself is a signal (a Balance concern)."
    The full summary is still returned — the warning is signal for the
    human, not a refusal.
    """
    paths = sorted(LEDGER_DIR.glob("*.json"))
    if not paths:
        return "_(ledger is empty)_\n"

    parts: list[str] = ["# Ledger Summary"]
    if active_scope:
        parts.append(f"_Active scope: `{active_scope}` "
                     f"(decision/attempt/completion entries on this scope "
                     f"are shown in summary form; others are compressed to one line.)_")
    else:
        parts.append("_No active scope specified — "
                     "all decision/attempt/completion entries compressed to one line. "
                     "Failure, repair, intention_shift, and boundary_change entries "
                     "are preserved in full regardless of scope._")
    parts.append("")

    n_full = 0
    n_active = 0
    n_compressed = 0

    for p in paths:
        try:
            entry = LedgerEntry(**json.loads(p.read_text(encoding="utf-8")))
        except (ValidationError, json.JSONDecodeError):
            continue

        if entry.type in _PRESERVE_IN_FULL:
            tags = ", ".join(entry.foundation_tag) if entry.foundation_tag else "—"
            priors = ", ".join(entry.prior_entries) if entry.prior_entries else "—"
            parts.append(
                f"## {entry.entry_id} · `{entry.author}` · **{entry.type}** "
                f"(conf {entry.confidence:.2f})"
            )
            parts.append(
                f"_scope: `{entry.scope}` · tags: {tags} · prior: {priors}_"
            )
            parts.append("")
            parts.append(f"**Summary:** {entry.summary}")
            if entry.detail:
                parts.append("")
                parts.append("**Detail:**")
                parts.append("")
                parts.append(entry.detail)
            parts.append("")
            n_full += 1
        elif active_scope is not None and entry.scope == active_scope:
            verdict_str = f" · verdict={entry.verdict}" if entry.verdict else ""
            parts.append(
                f"- **{entry.entry_id}** · `{entry.author}` · {entry.type}"
                f"{verdict_str} (conf {entry.confidence:.2f}) — {entry.summary}"
            )
            n_active += 1
        else:
            parts.append(
                f"- {entry.entry_id} · `{entry.author}` · {entry.type} · "
                f"`{entry.scope}` — {entry.summary}"
            )
            n_compressed += 1

    parts.append("")
    parts.append(
        f"_Counts: {n_full} preserved in full · "
        f"{n_active} on active scope · "
        f"{n_compressed} compressed_"
    )

    text = "\n".join(parts) + "\n"

    size = len(text.encode("utf-8"))
    if size > soft_limit_bytes:
        console.print(
            f"[yellow]Balance warning:[/] ledger summary is {size:,} bytes "
            f"(soft limit {soft_limit_bytes:,}). Per fnd-ledger.md, this is "
            f"a signal that the coordination may have grown beyond what its "
            f"current participants can hold."
        )

    return text


def latest_convergence_for_scope(entries: list[LedgerEntry]) -> LedgerEntry | None:
    """The most recent convergence decision for a scope.

    Convergence entries are decisions written by the role-holder when
    opening a multi-participant review; they have role_action=None
    (so they're distinguishable from take/release entries) and a
    summary that starts with "Convergence declared".
    """
    for e in reversed(entries):
        if (
            e.type == "decision"
            and e.role_action is None
            and e.summary.startswith("Convergence declared")
        ):
            return e
    return None


def current_orchestrator_for_scope(scope_rel: str) -> str | None:
    """Walk the ledger for the most recent unmatched take_orchestrator entry.

    Returns the identifier of the participant currently holding the
    orchestrator role for this scope, or None if no one holds it.

    The role-state machine is simple: a take_orchestrator entry begins a
    holding period; the next release_orchestrator entry by the same
    participant ends it. Multiple consecutive takes by different
    participants without a release are not valid (take_role enforces this).
    """
    entries = entries_for_scope(scope_rel)
    holder: str | None = None
    for e in entries:
        if e.role_action == "take_orchestrator":
            holder = e.author
        elif e.role_action == "release_orchestrator" and e.author == holder:
            holder = None
    return holder


def write_take_orchestrator_role(
    repo: Repo,
    scope_rel: str,
    participant: dict,
    acknowledging_release: LedgerEntry | None = None,
) -> LedgerEntry:
    """Record that a participant is taking the orchestrator role for a scope.

    Per fnd-field.md: 'A designated participant (human or agent) takes the
    orchestrator role for a defined scope. This role is declared in the
    ledger and carries a scope boundary — the orchestrator governs *this
    workflow*, not the entire coordination.'

    If `acknowledging_release` is provided, this take is part of a transfer
    per fnd-participants.md → Transfer: the new holder is acknowledging
    receipt of state from the prior holder. The prior_entries link captures
    the lineage chain.
    """
    prior_entries = []
    extra_detail = ""
    if acknowledging_release is not None:
        prior_entries = [acknowledging_release.entry_id]
        extra_detail = (
            f"\n\n## Acknowledging Transfer\n\n"
            f"This take entry acknowledges the state transfer from "
            f"`{acknowledging_release.author}` recorded in entry "
            f"{acknowledging_release.entry_id}. Per fnd-participants.md → "
            f"Transfer: 'The incoming participant acknowledges receipt before "
            f"assuming ownership.' This entry is that acknowledgment. The "
            f"state snapshot from the prior holder is in the linked release "
            f"entry's detail field — `{participant['identifier']}` has read "
            f"it and is taking the role with awareness of where things stand."
        )
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="decision",
        scope=scope_rel,
        prior_entries=prior_entries,
        summary=(
            f"`{participant['identifier']}` takes the orchestrator role for "
            f"{scope_rel}. Routing decisions on this scope will be attributed "
            f"to this participant until release."
        ),
        detail=(
            f"# Orchestrator role acquired\n\n"
            f"**Participant:** `{participant['identifier']}` "
            f"(steward: {participant.get('steward', '?')})\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Per fnd-field.md:**\n\n"
            f"> A designated participant (human or agent) takes the orchestrator "
            f"role for a defined scope. This role is declared in the ledger and "
            f"carries a scope boundary — the orchestrator governs *this workflow*, "
            f"not the entire coordination.\n\n"
            f"> The orchestrator is a participant, not a supervisor. It has a "
            f"declaration. It has boundaries. It can be refused. It can be "
            f"replaced. It is subject to every foundation, including Choice — it "
            f"proposes tasks, it does not impose them.\n\n"
            f"While this role is held, `review` and `repair` operations on this "
            f"scope are routed by `{participant['identifier']}`. The script "
            f"orchestrator.py is the tool they use to do that work; the script "
            f"itself is not the orchestrator. To release the role, run:\n\n"
            f"    python orchestrator.py release-role --scope {scope_rel} "
            f"--as {participant['identifier']}"
            f"{extra_detail}"
        ),
        confidence=1.0,
        foundation_tag=["boundaries", "intention"],
        role_action="take_orchestrator",
    )
    write_entry(entry, repo)
    return entry


def write_release_orchestrator_role(
    repo: Repo,
    scope_rel: str,
    participant: dict,
    reason: str = "voluntary release",
    snapshot: str | None = None,
    transferring_to: str | None = None,
) -> LedgerEntry:
    """Record that a participant is releasing the orchestrator role.

    If `snapshot` is provided, the release is part of a transfer per
    fnd-participants.md → Transfer: 'The outgoing participant writes a
    state snapshot to the ledger — what was done, what remains, what was
    learned.' The snapshot becomes part of the entry's detail under a
    State Snapshot section. The receiving participant acknowledges this
    release via take-role --acknowledging.
    """
    snapshot_section = ""
    transfer_intro = ""
    if snapshot is not None:
        snapshot_section = (
            f"\n\n## State Snapshot\n\n{snapshot}\n\n"
            f"Per fnd-participants.md → Transfer, the outgoing participant "
            f"writes this snapshot so the next holder can pick up scope from "
            f"a known state, not from a guess. The transfer is not complete "
            f"until the receiving participant runs:\n\n"
            f"    python orchestrator.py take-role --scope {scope_rel} "
            f"--as <recipient> --acknowledging {next_entry_id()}\n\n"
            f"Until that acknowledgment, the role is unheld and the scope is "
            f"in transition."
        )
    if transferring_to:
        transfer_intro = f"\n\n**Transferring to:** `{transferring_to}`"

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="decision",
        scope=scope_rel,
        prior_entries=[],
        summary=(
            f"`{participant['identifier']}` releases the orchestrator role for "
            f"{scope_rel}. Reason: {reason}."
            + (f" Transferring to `{transferring_to}` (pending acknowledgment)." if transferring_to else "")
        ),
        detail=(
            f"# Orchestrator role released\n\n"
            f"**Participant:** `{participant['identifier']}`\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Reason:** {reason}"
            f"{transfer_intro}\n\n"
            f"Per fnd-field.md, when no participant holds the orchestrator role "
            f"for a scope, the scope is in Infrastructure or Emergent Mode. In "
            f"Infrastructure Mode no work happens. In Emergent Mode, participants "
            f"self-select from the ledger.\n\n"
            f"To take the role again, any participant may run:\n\n"
            f"    python orchestrator.py take-role --scope {scope_rel} --as <participant>"
            f"{snapshot_section}"
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries"],
        role_action="release_orchestrator",
    )
    write_entry(entry, repo)
    return entry


def cmd_take_role(
    scope_rel: str,
    participant_id: str,
    acknowledging_release_id: str | None = None,
) -> int:
    """`take-role` subcommand. If acknowledging_release_id is provided, this
    is the second step of a transfer per fnd-participants.md → Transfer.
    """
    declarations = load_declarations()
    participant = next((d for d in declarations if d["identifier"] == participant_id), None)
    if participant is None:
        console.print(
            f"[red]error:[/] no declaration found for `{participant_id}`. "
            f"Add a declaration in `participants/declarations/` first."
        )
        return 2

    current = current_orchestrator_for_scope(scope_rel)
    if current is not None:
        console.print(
            Panel(
                f"[bold red]Cannot take orchestrator role on {scope_rel}.[/]\n\n"
                f"`{current}` already holds the role for this scope. Per fnd-field.md, "
                f"the role is exclusive — it must be released or transferred before "
                f"another participant can take it.\n\n"
                f"To release: [cyan]python orchestrator.py release-role --scope "
                f"{scope_rel} --as {current}[/]\n\n"
                f"For a transfer with state snapshot, use:\n"
                f"  [cyan]release-role --as {current} --snapshot @path/to/snapshot.md "
                f"--reason 'transferring to {participant_id}'[/]\n"
                f"  [cyan]take-role --as {participant_id} --acknowledging <release-entry-id>[/]",
                title="role conflict",
                border_style="red",
            )
        )
        return 1

    acknowledging_release: LedgerEntry | None = None
    if acknowledging_release_id is not None:
        try:
            acknowledging_release = load_entry(acknowledging_release_id)
        except FileNotFoundError:
            console.print(
                f"[red]error:[/] no ledger entry found with id "
                f"`{acknowledging_release_id}` to acknowledge."
            )
            return 2
        if acknowledging_release.role_action != "release_orchestrator":
            console.print(
                f"[red]error:[/] entry {acknowledging_release_id} is not a "
                f"release_orchestrator entry (role_action="
                f"{acknowledging_release.role_action!r}). Cannot acknowledge "
                f"a non-release as a transfer."
            )
            return 2
        if acknowledging_release.scope != scope_rel:
            console.print(
                f"[red]error:[/] entry {acknowledging_release_id} is for scope "
                f"`{acknowledging_release.scope}`, not `{scope_rel}`. Transfers "
                f"are scope-bounded."
            )
            return 2

    repo = get_repo()
    entry = write_take_orchestrator_role(repo, scope_rel, participant, acknowledging_release)
    transfer_note = ""
    if acknowledging_release is not None:
        transfer_note = (
            f"\n\nThis take acknowledges the transfer from "
            f"`{acknowledging_release.author}` (entry "
            f"{acknowledging_release.entry_id}). The transfer is now complete."
        )
    console.print(
        Panel(
            f"[bold green]Orchestrator role acquired.[/]\n\n"
            f"`{participant_id}` now holds the orchestrator role for "
            f"[bold]{scope_rel}[/].\n\n"
            f"Recorded as decision entry [bold]{entry.entry_id}[/].\n\n"
            f"While the role is held, the script will route review/repair tasks "
            f"on this scope and attribute orchestration entries to "
            f"`{participant_id}`.{transfer_note}",
            title=f"take-role: {entry.entry_id}",
            border_style="green",
        )
    )
    return 0


def write_self_selection_attempt(
    repo: Repo,
    scope_rel: str,
    participant: dict,
    reason: str | None,
) -> LedgerEntry:
    """Record a participant picking up scope from the ledger in Emergent Mode.

    Per fnd-participants.md → Accept and fnd-field.md → Emergent Mode:
    'No one holds a special role. Participants read the ledger, identify
    where they can contribute, propose their involvement via signal, and
    begin work when acknowledged.'

    The attempt entry is the participant's recorded acceptance of the
    scope. Future work the participant produces should link back to this
    entry via prior_entries.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="attempt",
        scope=scope_rel,
        prior_entries=[],
        summary=(
            f"`{participant['identifier']}` self-selects scope {scope_rel} "
            f"in Emergent Mode."
            + (f" Reason: {reason}." if reason else "")
        ),
        detail=(
            f"# Self-selection (Emergent Mode)\n\n"
            f"**Participant:** `{participant['identifier']}` "
            f"(steward: {participant.get('steward', '?')})\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            + (f"**Reason for self-selecting:** {reason}\n\n" if reason else "")
            + f"Per fnd-field.md → Emergent Mode: 'No one holds a special role. "
            f"Participants read the ledger, identify where they can contribute, "
            f"propose their involvement via signal, and begin work when "
            f"acknowledged.' This entry is `{participant['identifier']}`'s "
            f"declared involvement.\n\n"
            f"Per fnd-participants.md → Accept: 'Acceptance is recorded as an "
            f"`attempt` entry in the ledger. The participant now holds "
            f"ownership of the accepted scope.'\n\n"
            f"Future work this participant produces on this scope should link "
            f"back to this attempt entry via prior_entries — that's how the "
            f"Recursion foundation traces self-selected work."
        ),
        confidence=0.8,
        foundation_tag=["choice", "intention"],
    )
    write_entry(entry, repo)
    return entry


def cmd_self_select(scope_rel: str, participant_id: str, reason: str | None) -> int:
    """`self-select` subcommand. Refuses if a role-holder exists for the scope.

    Self-selection is an Emergent Mode action. If a participant currently
    holds the orchestrator role for this scope, the scope is in
    Orchestrated Mode and routing happens through that participant — you
    don't self-select around them, you ask them to route to you.
    """
    declarations = load_declarations()
    participant = next((d for d in declarations if d["identifier"] == participant_id), None)
    if participant is None:
        console.print(
            f"[red]error:[/] no declaration found for `{participant_id}`. "
            f"Add a declaration in `participants/declarations/` first."
        )
        return 2

    role_holder = current_orchestrator_for_scope(scope_rel)
    if role_holder is not None:
        console.print(
            Panel(
                f"[bold red]Cannot self-select on {scope_rel}.[/]\n\n"
                f"`{role_holder}` currently holds the orchestrator role for this "
                f"scope. The scope is in Orchestrated Mode — routing happens "
                f"through the role-holder, not via self-selection.\n\n"
                f"Self-selection only applies in Emergent or Infrastructure Mode "
                f"(no role-holder). Either:\n"
                f"  - Ask `{role_holder}` to route work to you, or\n"
                f"  - Wait for them to release the role, then self-select",
                title="scope is orchestrated",
                border_style="red",
            )
        )
        return 1

    repo = get_repo()
    entry = write_self_selection_attempt(repo, scope_rel, participant, reason)
    console.print(
        Panel(
            f"[bold green]Self-selection recorded.[/]\n\n"
            f"`{participant_id}` has picked up [bold]{scope_rel}[/] in "
            f"Emergent Mode.\n\n"
            f"Recorded as attempt entry [bold]{entry.entry_id}[/].\n\n"
            f"This entry IS the work-acceptance. Subsequent entries this "
            f"participant produces on this scope should link back via "
            f"prior_entries to {entry.entry_id} so the Recursion chain is "
            f"traceable.",
            title=f"self-select: {entry.entry_id}",
            border_style="green",
        )
    )
    return 0


def cmd_release_role(
    scope_rel: str,
    participant_id: str,
    reason: str = "voluntary release",
    snapshot: str | None = None,
    transferring_to: str | None = None,
) -> int:
    """`release-role` subcommand. If snapshot is provided, this is the first
    step of a transfer per fnd-participants.md → Transfer.
    """
    declarations = load_declarations()
    participant = next((d for d in declarations if d["identifier"] == participant_id), None)
    if participant is None:
        console.print(
            f"[red]error:[/] no declaration found for `{participant_id}`."
        )
        return 2

    current = current_orchestrator_for_scope(scope_rel)
    if current is None:
        console.print(
            f"[yellow]No orchestrator role currently held on {scope_rel} — "
            f"nothing to release.[/]"
        )
        return 0
    if current != participant_id:
        console.print(
            Panel(
                f"[bold red]Cannot release orchestrator role on {scope_rel}.[/]\n\n"
                f"`{current}` holds the role, not `{participant_id}`. Per "
                f"fnd-participants.md, a participant cannot release a role they "
                f"do not hold — that would be writing on behalf of another "
                f"participant.",
                title="not your role to release",
                border_style="red",
            )
        )
        return 1

    # Resolve --snapshot @path/to/file syntax
    snapshot_text = snapshot
    if snapshot is not None and snapshot.startswith("@"):
        snapshot_path = Path(snapshot[1:])
        if not snapshot_path.is_absolute():
            snapshot_path = ROOT / snapshot_path
        if not snapshot_path.exists():
            console.print(f"[red]error:[/] snapshot file not found: {snapshot_path}")
            return 2
        snapshot_text = snapshot_path.read_text(encoding="utf-8")

    if transferring_to is not None:
        # Verify the recipient exists, but do NOT verify their consent — they
        # acknowledge via take-role --acknowledging in their own command.
        recipient = next(
            (d for d in declarations if d["identifier"] == transferring_to), None
        )
        if recipient is None:
            console.print(
                f"[red]error:[/] transfer recipient `{transferring_to}` has no "
                f"declaration in `participants/declarations/`."
            )
            return 2

    repo = get_repo()
    entry = write_release_orchestrator_role(
        repo, scope_rel, participant, reason, snapshot_text, transferring_to
    )
    panel_title = f"release-role: {entry.entry_id}"
    if snapshot_text or transferring_to:
        panel_title = f"release-for-transfer: {entry.entry_id}"
        next_step = (
            f"\n\nTransfer pending. The recipient must acknowledge by running:\n"
            f"  [cyan]python orchestrator.py take-role --scope {scope_rel} "
            f"--as {transferring_to or '<recipient>'} --acknowledging {entry.entry_id}[/]\n\n"
            f"Until acknowledgment, the role is unheld and the scope is in "
            f"transition. Routing operations on this scope will be refused."
        )
    else:
        next_step = (
            f"\n\nThe scope is now without an orchestrator. To route work on it "
            f"again, take the role first."
        )
    console.print(
        Panel(
            f"[bold yellow]Orchestrator role released.[/]\n\n"
            f"`{participant_id}` released the role for [bold]{scope_rel}[/].\n\n"
            f"Recorded as decision entry [bold]{entry.entry_id}[/].{next_step}",
            title=panel_title,
            border_style="yellow",
        )
    )
    return 0


def is_scope_safe_to_synthesize(entries: list[LedgerEntry]) -> tuple[bool, str]:
    """Refuse to synthesize over an unresolved fired breaker.

    A failure entry on the scope without a repair entry linking back is an
    open conflict. Synthesizing over it would route around a fired circuit
    breaker — exactly what fnd-field.md forbids.
    """
    failure_ids = {e.entry_id for e in entries if e.type == "failure"}
    if not failure_ids:
        return True, ""
    repaired = set()
    for e in entries:
        if e.type == "repair":
            for pid in e.prior_entries:
                if pid in failure_ids:
                    repaired.add(pid)
    unresolved = failure_ids - repaired
    if unresolved:
        return False, (
            f"unresolved failure entries on this scope: {sorted(unresolved)}. "
            f"Run `python orchestrator.py repair --failure-entry <id>` first."
        )
    return True, ""


def unresolved_failures_for_scope(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Return failure entries on the scope that have no repair linking back."""
    failure_by_id = {e.entry_id: e for e in entries if e.type == "failure"}
    repaired_ids: set[str] = set()
    for e in entries:
        if e.type == "repair":
            for pid in e.prior_entries:
                if pid in failure_by_id:
                    repaired_ids.add(pid)
    return [failure_by_id[fid] for fid in failure_by_id if fid not in repaired_ids]


def repetition_breaker_should_fire(entries: list[LedgerEntry]) -> bool:
    """The Repetition circuit breaker, in its first form.

    The framework's strict definition (fnd-failure.md) is '3+ attempt entries
    on the same scope without an intervening completion, failure, or repair
    entry.' In our orchestrator that translates approximately to: 3+
    unresolved failures on the same scope.

    A failure is 'resolved' when a repair entry links back to it via
    prior_entries. Three unresolved failures means the scope has gone wrong
    three times in a row without anyone diagnosing and resolving the
    breakage. The breaker exists to prevent grinding repetition without
    learning — the most common Recursion failure when nothing changes
    between attempts.
    """
    return len(unresolved_failures_for_scope(entries)) >= 3


def write_repetition_failure(
    repo: Repo,
    scope_path: str,
    unresolved_failures: list[LedgerEntry],
    role_holder: str,
) -> LedgerEntry:
    """Write the repetition_breaker failure entry."""
    failure_lines = "\n".join(
        f"- entry {e.entry_id} ({e.type} from `{e.author}`): {e.summary[:100]}"
        for e in unresolved_failures
    )
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[e.entry_id for e in unresolved_failures],
        summary=(
            f"Repetition circuit breaker fired on {scope_path}: "
            f"{len(unresolved_failures)} unrepaired failures accumulated. "
            f"Refusing further routing until repair."
        ),
        detail=(
            f"# Repetition circuit breaker fired\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Unresolved failures:**\n\n{failure_lines}\n\n"
            f"Per fnd-failure.md, the Repetition circuit breaker fires when "
            f"3+ attempts on the same scope have failed without intervening "
            f"resolution. Continuing to route the same task to the same "
            f"participants under the same conditions is a Recursion failure: "
            f"the coordination has no memory of what it just tried.\n\n"
            f"This entry pauses routing on this scope. To proceed:\n\n"
            f"  1. Run repair on each unresolved failure above:\n"
            + "\n".join(
                f"     [cyan]python orchestrator.py repair --failure-entry {e.entry_id}[/]"
                for e in unresolved_failures
            )
            + "\n\n"
            f"  2. Or release the role and let participants self-select with "
            f"a different framing in Emergent Mode.\n\n"
            f"  3. Or — if the diagnosis is that the scope itself is wrong — "
            f"write an `intention_shift` entry redefining what's being attempted."
        ),
        confidence=1.0,
        foundation_tag=["recursion", "balance"],
    )
    write_entry(entry, repo)
    return entry


# ---------- Resource + Timeout circuit breakers ----------

# Session-scoped token accumulator. Resets on script restart, which matches
# the framework's concept of a "session." Keyed by participant identifier.
_session_token_usage: dict[str, int] = {}


def record_token_usage(participant_id: str, tokens: int) -> None:
    """Accumulate total tokens used by a participant in this session."""
    _session_token_usage[participant_id] = _session_token_usage.get(participant_id, 0) + tokens


def check_resource_breaker(
    config: dict,
    scope_path: str,
    role_holder: str,
    repo: Repo | None,
) -> LedgerEntry | None:
    """The Resource circuit breaker.

    Per fnd-failure.md: fires when one participant's resource consumption
    exceeds N× the per-participant average (N = config circuit_breakers.resource_multiplier,
    default 2.0). Also fires if a participant exceeds their declared
    resource_ceiling.max_tokens_per_session.

    Returns the failure entry if the breaker fires, None otherwise.
    """
    if not _session_token_usage:
        return None

    multiplier = config.get("circuit_breakers", {}).get("resource_multiplier", 2.0)
    total = sum(_session_token_usage.values())
    n_participants = len(_session_token_usage)
    average = total / n_participants if n_participants > 0 else 0
    threshold = average * multiplier

    # Check per-participant average breach
    offender_id: str | None = None
    offender_tokens = 0
    for pid, tokens in _session_token_usage.items():
        if tokens > threshold and threshold > 0:
            offender_id = pid
            offender_tokens = tokens
            break

    if offender_id is None:
        return None

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[],
        summary=(
            f"Resource circuit breaker fired: `{offender_id}` used {offender_tokens:,} "
            f"tokens ({offender_tokens / average:.1f}× the {average:,.0f}-token average). "
            f"Threshold: {multiplier}×."
        ),
        detail=(
            f"# Resource circuit breaker fired\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Offender:** `{offender_id}` — {offender_tokens:,} tokens\n\n"
            f"**Average across {n_participants} participants:** {average:,.0f} tokens\n\n"
            f"**Multiplier threshold:** {multiplier}× = {threshold:,.0f} tokens\n\n"
            f"**Session usage by participant:**\n\n"
            + "\n".join(f"- `{pid}`: {t:,} tokens" for pid, t in _session_token_usage.items())
            + "\n\n"
            f"Per fnd-failure.md, the Resource circuit breaker fires when one "
            f"participant's consumption exceeds {multiplier}× the per-participant "
            f"average. This is a Balance concern — disproportionate resource usage "
            f"means the coordination is under-utilizing some participants and "
            f"over-utilizing others."
        ),
        confidence=1.0,
        foundation_tag=["balance"],
    )
    write_entry(entry, repo)
    return entry


def check_resource_ceiling(
    participant_id: str,
    declarations: list[dict],
    scope_path: str,
    role_holder: str,
    repo: Repo | None,
) -> LedgerEntry | None:
    """Check if a participant has exceeded their declared max_tokens_per_session."""
    tokens_used = _session_token_usage.get(participant_id, 0)
    decl = next((d for d in declarations if d["identifier"] == participant_id), None)
    if decl is None:
        return None
    ceiling = (decl.get("resource_ceiling") or {}).get("max_tokens_per_session")
    if ceiling is None or tokens_used <= ceiling:
        return None

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[],
        summary=(
            f"Resource ceiling breached: `{participant_id}` used {tokens_used:,} "
            f"tokens, exceeding their declared ceiling of {ceiling:,}."
        ),
        detail=(
            f"# Resource ceiling breached\n\n"
            f"**Participant:** `{participant_id}`\n\n"
            f"**Tokens used this session:** {tokens_used:,}\n\n"
            f"**Declared ceiling:** {ceiling:,} (resource_ceiling.max_tokens_per_session)\n\n"
            f"The participant's declaration sets a resource ceiling. Continuing to "
            f"route work to this participant would violate their declared Boundaries."
        ),
        confidence=1.0,
        foundation_tag=["balance", "boundaries"],
    )
    write_entry(entry, repo)
    return entry


def check_timeout_breaker(repo: Repo | None) -> list[LedgerEntry]:
    """The Timeout circuit breaker.

    Scans signal/archive/ for signals that have gone unacknowledged past
    the destination participant's declared latency_tolerance_seconds.

    A signal is "acknowledged" when there is an acknowledgment signal in
    archive whose lineage includes the original signal's id.

    Returns any failure entries written (one per timed-out signal).
    """
    _ensure_signal_dirs()
    declarations = load_declarations()
    decl_map = {d["identifier"]: d for d in declarations}

    # Build set of all acknowledged signal ids
    acked_ids: set[str] = set()
    for p in sorted(SIGNAL_ARCHIVE.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if data.get("type") == "acknowledgment":
                for sid in data.get("lineage", []):
                    acked_ids.add(sid)
        except (json.JSONDecodeError, KeyError):
            continue

    now = datetime.now(timezone.utc)
    failures: list[LedgerEntry] = []

    for p in sorted(SIGNAL_ARCHIVE.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue

        sig_id = data.get("signal_id", "")
        sig_type = data.get("type", "")
        destination = data.get("destination", "")
        ts_str = data.get("timestamp", "")

        # Skip acknowledgments, handoffs, and already-acked signals
        if sig_type in ("acknowledgment", "handoff"):
            continue
        if sig_id in acked_ids:
            continue

        # Check if destination has a latency tolerance
        dest_decl = decl_map.get(destination)
        if dest_decl is None:
            continue
        tolerance = (dest_decl.get("context_constraints") or {}).get("latency_tolerance_seconds")
        if tolerance is None:
            continue

        # Parse timestamp and check
        try:
            sig_time = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue

        elapsed = (now - sig_time).total_seconds()
        if elapsed <= tolerance:
            continue

        # Timeout breaker fires for this signal
        scope = (data.get("payload") or {}).get("scope", "unknown")
        entry = LedgerEntry(
            entry_id=next_entry_id(),
            timestamp=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            author=destination,
            type="failure",
            scope=scope,
            prior_entries=[],
            summary=(
                f"Timeout circuit breaker fired: signal {sig_id} to "
                f"`{destination}` unacknowledged for {elapsed:.0f}s "
                f"(tolerance: {tolerance}s)."
            ),
            detail=(
                f"# Timeout circuit breaker fired\n\n"
                f"**Signal id:** {sig_id}\n\n"
                f"**Type:** {sig_type}\n\n"
                f"**From:** `{data.get('origin', '?')}`\n\n"
                f"**To:** `{destination}`\n\n"
                f"**Sent:** {ts_str}\n\n"
                f"**Elapsed:** {elapsed:.0f}s\n\n"
                f"**Tolerance:** {tolerance}s "
                f"(from `{destination}` declaration context_constraints."
                f"latency_tolerance_seconds)\n\n"
                f"Per fnd-failure.md, the Timeout breaker fires when a signal "
                f"has not received acknowledgment within the declared latency "
                f"tolerance of the receiving participant. This may indicate the "
                f"participant is unresponsive (potential ungraceful departure) "
                f"or that the signal was lost."
            ),
            confidence=1.0,
            foundation_tag=["signal", "boundaries"],
        )
        write_entry(entry, repo)
        failures.append(entry)
        console.print(
            f"[red]Timeout breaker fired:[/] signal {sig_id} → "
            f"`{destination}` ({elapsed:.0f}s > {tolerance}s)"
        )

    return failures


def run_synthesis(scope_rel: str) -> int:
    config = load_config()
    declarations = load_declarations()
    intention = config.get("intention", "")

    scope_abs = ROOT / scope_rel
    if not scope_abs.exists():
        console.print(f"[red]error:[/] scope file not found: {scope_rel}")
        return 2
    scope_content = scope_abs.read_text(encoding="utf-8")

    # ---- Gate: opening synthesis is itself an orchestration decision ----
    # The act of transitioning a scope from Orchestrated to Emergent and
    # surfacing a question to all participants is something the role-holder
    # does. After the transition, the role is released — Emergent Mode is
    # roleless by definition.
    role_holder = current_orchestrator_for_scope(scope_rel)
    if role_holder is None:
        console.print(
            Panel(
                f"[bold red]Cannot open synthesis on {scope_rel}.[/]\n\n"
                f"Opening synthesis transitions the scope from Orchestrated to "
                f"Emergent Mode and surfaces a question to all participants. "
                f"That transition is itself an orchestration decision — it "
                f"requires a participant to be holding the role.\n\n"
                f"Take the role first:\n"
                f"  [cyan]python orchestrator.py take-role --scope {scope_rel} "
                f"--as <participant>[/]\n\n"
                f"The role will be auto-released as part of the transition, since "
                f"Emergent Mode has no role-holder.",
                title="no orchestrator",
                border_style="red",
            )
        )
        return 2

    role_holder_decl = next(
        (d for d in declarations if d["identifier"] == role_holder), None
    )
    if role_holder_decl is None:
        console.print(
            f"[red]error:[/] role holder `{role_holder}` is in the ledger but "
            f"has no declaration in `participants/declarations/`. Cannot proceed."
        )
        return 2

    entries = entries_for_scope(scope_rel)
    if not entries:
        console.print(f"[red]error:[/] no ledger entries for scope {scope_rel}")
        return 2

    safe, reason = is_scope_safe_to_synthesize(entries)
    if not safe:
        console.print(
            Panel(
                f"[bold red]Refusing to synthesize.[/]\n\n{reason}\n\n"
                f"Per fnd-field.md, the coordination 'never continues operating after a "
                f"circuit breaker fires without entering the repair cycle.' "
                f"Synthesis over an open failure would route around the breaker.",
                title="synthesis blocked",
                border_style="red",
            )
        )
        return 2

    convergence = latest_convergence_for_scope(entries)
    if convergence is None:
        console.print(
            f"[red]error:[/] no convergence decision found for {scope_rel}. "
            f"Run `python orchestrator.py review --scope {scope_rel}` first."
        )
        return 2

    completions = [
        e for e in entries
        if e.type == "completion" and convergence.entry_id in e.prior_entries
    ]
    if not completions:
        console.print(f"[red]error:[/] no completion entries linked to convergence {convergence.entry_id}")
        return 2

    repairs = [
        e for e in entries
        if e.type == "repair" and any(
            cid in e.prior_entries for cid in (c.entry_id for c in completions)
        )
    ]

    # All active participants are invited — no single synthesizer.
    invitees = [
        d for d in declarations
        if d.get("participation_mode") == "active" and d.get("litellm_model")
    ]
    if not invitees:
        console.print("[red]error:[/] no active participants with litellm_model to invite")
        return 2

    repo = get_repo()

    # ---- Mode transition: Orchestrated → Emergent ----
    synthesis_question = (
        f"Given the {len(completions)} convergent completion(s) on {scope_rel}, "
        f"what is the unified position the coordination should hold? Multiple "
        f"proposals are welcome; the aggregate is the synthesis."
    )
    transition_entry = write_mode_transition_decision(
        repo, scope_rel, "orchestrated", "emergent",
        f"Synthesis question opened for self-selection: {synthesis_question}",
        convergence.entry_id, role_holder,
    )
    intention_shift_entry = write_intention_shift(
        repo, scope_rel, synthesis_question, transition_entry.entry_id, role_holder,
    )

    # The transition out of Orchestrated Mode means the role is no longer
    # held — Emergent Mode is roleless. Auto-release as part of the
    # transition. The release entry is authored by the role-holder
    # themselves (their last act in the role on this scope).
    release_entry = write_release_orchestrator_role(
        repo, scope_rel, role_holder_decl,
        reason=(
            f"Auto-released as part of the orchestrated → emergent transition for "
            f"synthesis (transition entry {transition_entry.entry_id})"
        ),
    )
    console.print(
        f"[dim]role released: {release_entry.entry_id} (auto, transition to Emergent)[/]"
    )

    console.print(
        Panel.fit(
            f"[bold]Scope:[/] {scope_rel}\n"
            f"[bold]Mode:[/] orchestrated → [bold magenta]emergent[/]\n"
            f"[bold]Question:[/] {synthesis_question}\n"
            f"[bold]Transition entry:[/] {transition_entry.entry_id}  ·  "
            f"[bold]Intention shift:[/] {intention_shift_entry.entry_id}\n"
            f"[bold]Invited:[/] {', '.join(d['identifier'] for d in invitees)}",
            title="Emergent Synthesis",
        )
    )

    # ---- Foundations and shared context ----
    foundations_text = load_foundations([
        "fnd-preamble.md", "fnd-field.md", "fnd-ledger.md", "fnd-signal.md"
    ])

    completion_blocks = "\n\n".join(
        f"### entry {e.entry_id} — `{e.author}` (verdict: {e.verdict}, confidence: {e.confidence:.2f})\n\n"
        f"```json\n{e.model_dump_json(indent=2, exclude_none=True)}\n```"
        for e in completions
    )
    repair_blocks = "\n\n".join(
        f"### entry {e.entry_id} — `{e.author}` (verdict: {e.verdict}, confidence: {e.confidence:.2f})\n\n"
        f"```json\n{e.model_dump_json(indent=2, exclude_none=True)}\n```"
        for e in repairs
    ) if repairs else "*(none — no conflicts on this scope)*"

    lang = Path(scope_rel).suffix.lstrip(".") or "text"
    convergence_json = convergence.model_dump_json(indent=2, exclude_none=True)

    # Compute the ledger summary once and share it across all invitees.
    # The summary is the same for every participant in this synthesis pass,
    # and computing it inside the loop would also fire the Balance warning
    # once per invitee.
    ledger_summary_text = summarize_ledger(active_scope=scope_rel)

    proposals: list[LedgerEntry] = []
    refusals: list[LedgerEntry] = []
    invitee_results: list[dict[str, Any]] = []
    required_priors = (convergence.entry_id, intention_shift_entry.entry_id)
    required_priors_str = ", ".join(required_priors)

    for decl in invitees:
        author = decl["identifier"]
        console.print(f"\n[cyan]→ inviting {author} to self-select…[/]")

        system = SELF_SELECT_SYNTHESIS_SYSTEM_TEMPLATE.format(
            declaration_json=json.dumps(decl, indent=2),
            intention=intention,
            synthesis_question=synthesis_question,
            transition_id=transition_entry.entry_id,
            intention_shift_id=intention_shift_entry.entry_id,
            required_priors_str=required_priors_str,
            foundations=foundations_text,
            signal_docs=SIGNAL_ENVELOPE_DOCS,
        )
        user = SELF_SELECT_SYNTHESIS_USER_TEMPLATE.format(
            ledger_summary=ledger_summary_text,
            convergence_json=convergence_json,
            completion_blocks=completion_blocks,
            repair_blocks=repair_blocks,
            lang=lang,
            scope_content=scope_content,
        )
        base_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        entry, error = request_entry_with_retry(
            decl=decl,
            base_messages=base_messages,
            expected_types=("decision",),  # failure (refusal) is always also accepted
            required_prior_entries=required_priors,
            scope_path=scope_rel,
            repo=repo,
            # In Emergent Mode (post-transition) there is no role-holder.
            # The handoff origin is the participant being invited themselves
            # — they are self-selecting via the script as their tool.
            from_participant=decl["identifier"],
            handoff_task_type="synthesis_invitation",
        )

        if entry is None:
            console.print(f"  [red]✗ {author} could not produce a valid entry:[/] {error}")
            # In Emergent Mode (post-transition), there is no role-holder. The
            # observation that a participant couldn't produce an entry is
            # attributed to the participant themselves — they failed to
            # converge in the field-without-orchestrator, which is the
            # honest framing.
            failure = write_participant_failure(
                repo, scope_rel, decl, error or "unknown",
                intention_shift_entry.entry_id, decl["identifier"],
            )
            invitee_results.append({
                "author": author,
                "outcome": "failed",
                "entry_id": failure.entry_id,
                "summary": failure.summary,
            })
            continue

        write_entry(entry, repo)
        if entry.type == "decision":
            proposals.append(entry)
            console.print(
                f"  [green]✓[/] {author} proposed entry {entry.entry_id} "
                f"(verdict={entry.verdict or '—'}, confidence={entry.confidence:.2f})"
            )
            invitee_results.append({
                "author": author,
                "outcome": "proposed",
                "entry_id": entry.entry_id,
                "verdict": entry.verdict,
                "confidence": entry.confidence,
                "summary": entry.summary,
            })
        elif entry.type == "failure":
            refusals.append(entry)
            console.print(
                f"  [yellow]⊘[/] {author} refused entry {entry.entry_id}: {entry.summary}"
            )
            invitee_results.append({
                "author": author,
                "outcome": "refused",
                "entry_id": entry.entry_id,
                "summary": entry.summary,
            })

    print_synthesis_aggregation(scope_rel, proposals, refusals, invitee_results)

    # ---- Mode return: Emergent → Infrastructure ----
    # After synthesis completes (all invitees responded), the field returns
    # to Infrastructure mode. The scope is no longer under active
    # orchestrated or emergent work — it's at rest. The transition is
    # authored by the original role-holder who initiated synthesis; their
    # identity persists in the local scope even though the role was released.
    n_proposed = len(proposals)
    n_refused = len(refusals)
    n_failed = len([r for r in invitee_results if r.get("outcome") == "failed"])
    verdicts = [p.verdict for p in proposals if p.verdict]
    convergent = len(set(verdicts)) <= 1 and verdicts
    verdict_summary = (
        f"convergent ({verdicts[0]})" if convergent
        else f"divergent ({', '.join(set(verdicts))})" if verdicts
        else "no proposals"
    )
    synthesis_summary = (
        f"Synthesis completed. {n_proposed} proposal(s), {n_refused} refusal(s), "
        f"{n_failed} failure(s). Verdicts: {verdict_summary}."
    )

    mode_return_entry = write_mode_transition_decision(
        repo, scope_rel,
        from_mode="emergent",
        to_mode="infrastructure",
        reason=synthesis_summary,
        triggering_entry_id=transition_entry.entry_id,
        role_holder=role_holder,
    )
    console.print(
        f"[dim]mode return: {mode_return_entry.entry_id} "
        f"(emergent → infrastructure)[/]"
    )

    return 0


def print_synthesis_aggregation(
    scope_rel: str,
    proposals: list[LedgerEntry],
    refusals: list[LedgerEntry],
    invitee_results: list[dict[str, Any]],
) -> None:
    """Mechanical aggregation of self-selected synthesis proposals.

    No interpretation, no LLM call. The orchestrator counts verdicts, lists
    who proposed/refused, and surfaces convergence vs divergence among
    proposals. The human reads this panel + the proposal entries directly
    in the ledger.
    """
    n_invited = len(invitee_results)
    n_proposed = len(proposals)
    n_refused = len(refusals)
    n_failed = sum(1 for r in invitee_results if r["outcome"] == "failed")

    # Verdict aggregation
    verdict_counts: dict[str, int] = {}
    for p in proposals:
        v = p.verdict or "no_judgment"
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    convergent = len(verdict_counts) == 1 and n_proposed >= 2
    divergent = len(verdict_counts) >= 2

    # Build the aggregation table
    table = Table(title=f"Synthesis Aggregation — {scope_rel}", show_lines=True)
    table.add_column("Participant", style="bold")
    table.add_column("Outcome")
    table.add_column("Entry")
    table.add_column("Verdict")
    table.add_column("Conf")
    table.add_column("Summary", overflow="fold")
    for r in invitee_results:
        outcome_color = {
            "proposed": "green",
            "refused": "yellow",
            "failed": "red",
        }.get(r["outcome"], "")
        table.add_row(
            r["author"],
            f"[{outcome_color}]{r['outcome']}[/]",
            r["entry_id"],
            r.get("verdict") or "—",
            f"{r.get('confidence', 0):.2f}" if r.get("confidence") is not None else "—",
            r["summary"],
        )
    console.print(table)

    # Verdict aggregate panel
    if not proposals:
        verdict_text = "[dim]No proposals received.[/]"
    else:
        verdict_text = "\n".join(
            f"  • [bold]{v}[/]: {c}" for v, c in sorted(verdict_counts.items(), key=lambda kv: -kv[1])
        )

    if convergent:
        signal_line = (
            f"[bold green]Convergent:[/] all {n_proposed} proposals share verdict "
            f"`{next(iter(verdict_counts))}`. Strong signal."
        )
    elif divergent:
        signal_line = (
            f"[bold yellow]Divergent:[/] {n_proposed} proposals across "
            f"{len(verdict_counts)} verdicts. The aggregate IS the signal — "
            f"the human reads each proposal directly."
        )
    else:
        signal_line = "[dim]No verdict signal — see refusals and failures above.[/]"

    console.print(
        Panel(
            f"[bold]Invited:[/] {n_invited}  ·  "
            f"[bold]Proposed:[/] {n_proposed}  ·  "
            f"[bold]Refused:[/] {n_refused}  ·  "
            f"[bold]Failed:[/] {n_failed}\n\n"
            f"[bold]Verdict counts:[/]\n{verdict_text}\n\n"
            f"{signal_line}\n\n"
            f"[dim]The orchestrator does not write a unified decision entry. The "
            f"proposals above ARE the synthesis; the human reads them and any "
            f"refusals to form a position.[/]",
            title="Aggregate (Infrastructure Mode summary)",
            border_style="cyan",
        )
    )


# ---------- Output ----------

def print_summary(results: list[dict[str, Any]]) -> None:
    table = Table(title="Review Results", show_lines=True)
    table.add_column("Author", style="bold")
    table.add_column("Entry")
    table.add_column("Type")
    table.add_column("Verdict")
    table.add_column("Conf")
    table.add_column("Tokens (in/out)")
    table.add_column("Summary / Error", overflow="fold")
    for r in results:
        if r.get("error"):
            table.add_row(r["author"], "—", "—", "—", "—", "—", f"[red]{r['error']}[/]")
            continue
        tokens = f"{r.get('tokens_in') or '?'} / {r.get('tokens_out') or '?'}"
        table.add_row(
            r["author"],
            r["entry_id"],
            r["type"],
            r.get("verdict") or "—",
            f"{r['confidence']:.2f}",
            tokens,
            r["summary"],
        )
    console.print(table)


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(prog="orchestrator", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_review = sub.add_parser(
        "review",
        help="Send a scope artifact to agents (capability-routed if --task-type or inferred)",
    )
    p_review.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the artifact to review",
    )
    p_review.add_argument(
        "--task-type",
        default=None,
        help="Task type for capability-based routing (e.g., code_review, writing_review). "
             "If omitted, inferred from the scope file extension. Agents whose "
             "preferred_tasks include this type are preferred; if none match, all "
             "active agents review the scope (backward compatible broadcast).",
    )

    p_repair = sub.add_parser(
        "repair", help="Run the repair cycle for a fired failure entry"
    )
    p_repair.add_argument(
        "--failure-entry",
        required=True,
        help="entry_id of the failure that fired the breaker (e.g. '004')",
    )
    p_repair.add_argument(
        "--arbiter",
        default=None,
        help="Identifier of the participant to act as arbiter (defaults to config.convergence.arbiter)",
    )
    p_repair.add_argument(
        "--verify",
        action="store_true",
        help="After repair, automatically re-run the original failing reviewers "
             "under the resolved conditions. Per fnd-repair.md: verification "
             "must include a limited rerun or explicitly record why rerun is "
             "impossible or unsafe.",
    )

    p_synth = sub.add_parser(
        "synthesize",
        help="Open emergent-mode synthesis on a scope; all active participants self-select",
    )
    p_synth.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the artifact to synthesize",
    )

    p_take = sub.add_parser(
        "take-role",
        help="Declare that a participant takes the orchestrator role for a scope",
    )
    p_take.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the scope to orchestrate",
    )
    p_take.add_argument(
        "--as",
        dest="as_participant",
        required=True,
        help="Identifier of the participant taking the role",
    )
    p_take.add_argument(
        "--acknowledging",
        default=None,
        help="Optional: entry_id of a release_orchestrator entry being acknowledged. "
             "Use this when the take is the second step of a transfer.",
    )

    p_release = sub.add_parser(
        "release-role",
        help="Declare that a participant releases the orchestrator role for a scope",
    )
    p_release.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the scope to release",
    )
    p_release.add_argument(
        "--as",
        dest="as_participant",
        required=True,
        help="Identifier of the participant releasing the role (must be the current holder)",
    )
    p_release.add_argument(
        "--reason",
        default="voluntary release",
        help="Free-text reason for release",
    )
    p_release.add_argument(
        "--snapshot",
        default=None,
        help="State snapshot for transfer per fnd-participants.md → Transfer. "
             "String literal, or @path/to/file.md to read from disk.",
    )
    p_release.add_argument(
        "--to",
        dest="transferring_to",
        default=None,
        help="Identifier of the participant the role is being transferred to. "
             "They must acknowledge via take-role --acknowledging.",
    )

    p_self = sub.add_parser(
        "self-select",
        help="A participant declares they are picking up scope from the ledger (Emergent Mode)",
    )
    p_self.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the scope being picked up",
    )
    p_self.add_argument(
        "--as",
        dest="as_participant",
        required=True,
        help="Identifier of the participant self-selecting",
    )
    p_self.add_argument(
        "--reason",
        default=None,
        help="Optional: why this participant is picking up this scope",
    )

    p_ledger = sub.add_parser(
        "ledger",
        help="Print ledger entries (full panels by default; --summary for the compressed view)",
    )
    p_ledger.add_argument(
        "--summary",
        action="store_true",
        help="Print the compressed ledger view per fnd-ledger.md → Ledger Summary "
             "instead of one panel per entry. Failure/repair/intention_shift/"
             "boundary_change entries are preserved in full; other types are "
             "compressed unless their scope matches --scope.",
    )
    p_ledger.add_argument(
        "--scope",
        default=None,
        help="(--summary only) Treat this scope as active: decision/attempt/"
             "completion entries on it are shown in summary form rather than "
             "compressed to one line.",
    )

    p_inbox = sub.add_parser(
        "inbox",
        help="List or process signal envelopes in signal/inbox/ and signal/archive/",
    )
    p_inbox.add_argument(
        "action",
        choices=["list", "process"],
        help="`list` shows pending and archived signals; `process` dispatches all pending signals via their handlers (offline-friendly — no API calls).",
    )

    args = parser.parse_args()
    if args.cmd == "review":
        return run_review(args.scope, task_type=args.task_type)
    if args.cmd == "repair":
        return run_repair(args.failure_entry, args.arbiter, verify=args.verify)
    if args.cmd == "synthesize":
        return run_synthesis(args.scope)
    if args.cmd == "take-role":
        return cmd_take_role(args.scope, args.as_participant, args.acknowledging)
    if args.cmd == "release-role":
        return cmd_release_role(
            args.scope, args.as_participant, args.reason,
            args.snapshot, args.transferring_to,
        )
    if args.cmd == "self-select":
        return cmd_self_select(args.scope, args.as_participant, args.reason)
    if args.cmd == "ledger":
        if args.summary:
            return print_ledger_summary(args.scope)
        return print_ledger()
    if args.cmd == "inbox":
        if args.action == "list":
            return inbox_list()
        if args.action == "process":
            return inbox_process()
    return 2


def inbox_list() -> int:
    _ensure_signal_dirs()
    pending = sorted(SIGNAL_INBOX.glob("*.json"))
    archived = sorted(SIGNAL_ARCHIVE.glob("*.json"))

    if not pending and not archived:
        console.print("[dim]signal inbox and archive are empty[/]")
        return 0

    if pending:
        console.print(f"\n[bold yellow]Pending in signal/inbox/[/] ({len(pending)}):")
        for p in pending:
            try:
                env = SignalEnvelope(**json.loads(p.read_text(encoding="utf-8")))
                console.print(
                    f"  • [bold]{env.signal_id}[/]  type={env.type}  "
                    f"from={env.origin} → {env.destination}  conf={env.confidence:.2f}"
                )
                console.print(f"    [dim]{env.context_summary}[/]")
            except (ValidationError, json.JSONDecodeError) as e:
                console.print(f"  • [red]{p.name}[/] (invalid: {e})")

    if archived:
        console.print(f"\n[bold cyan]Archived in signal/archive/[/] ({len(archived)}):")
        for p in archived:
            try:
                env = SignalEnvelope(**json.loads(p.read_text(encoding="utf-8")))
                console.print(
                    f"  • [bold]{env.signal_id}[/]  type={env.type}  "
                    f"from={env.origin}  conf={env.confidence:.2f}"
                )
                console.print(f"    [dim]{env.context_summary}[/]")
            except (ValidationError, json.JSONDecodeError) as e:
                console.print(f"  • [red]{p.name}[/] (invalid: {e})")

    return 0


def inbox_process() -> int:
    """Process every signal currently in signal/inbox/.

    This is the offline test entry point: drop a hand-written JSON envelope
    into signal/inbox/, run this command, and watch it dispatch through the
    handlers without any API calls.
    """
    _ensure_signal_dirs()
    pending = sorted(SIGNAL_INBOX.glob("*.json"))
    if not pending:
        console.print("[dim]signal/inbox/ is empty — nothing to process[/]")
        return 0

    repo = get_repo()
    n_processed = 0
    n_failed = 0

    console.print(f"[bold]Processing {len(pending)} signal(s) from inbox…[/]\n")

    for p in pending:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            envelope = SignalEnvelope(**data)
        except (ValidationError, json.JSONDecodeError) as e:
            console.print(f"[red]✗ {p.name} is invalid:[/] {e}")
            n_failed += 1
            continue

        try:
            handler = SIGNAL_HANDLERS.get(envelope.type, handle_default)
            handler(envelope, repo)
            archive_signal(envelope, repo)
            n_processed += 1
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]✗ handler error on {envelope.signal_id}:[/] {e}")
            n_failed += 1

    console.print(
        f"\n[bold]Done.[/] Processed: {n_processed}  ·  Failed: {n_failed}"
    )

    # Run the Timeout circuit breaker after processing all pending signals
    repo = get_repo()
    timeout_failures = check_timeout_breaker(repo)
    if timeout_failures:
        console.print(
            f"\n[bold red]Timeout breaker fired for {len(timeout_failures)} "
            f"signal(s).[/] Run `python orchestrator.py ledger` to see failure entries."
        )

    return 0 if n_failed == 0 else 1


def print_ledger() -> int:
    entries = sorted(LEDGER_DIR.glob("*.json"))
    if not entries:
        console.print("[dim]ledger is empty[/]")
        return 0
    for p in entries:
        data = json.loads(p.read_text(encoding="utf-8"))
        console.print(
            Panel(
                f"[bold]{data['author']}[/]  ·  {data['type']}  ·  conf {data['confidence']:.2f}\n"
                f"[dim]{data['timestamp']}  ·  scope: {data['scope']}[/]\n\n"
                f"{data['summary']}",
                title=f"entry {data['entry_id']}",
            )
        )
    return 0


def print_ledger_summary(active_scope: str | None) -> int:
    """Render the compressed ledger view (per fnd-ledger.md → Ledger Summary).

    The summary text itself is generated by `summarize_ledger`; this wrapper
    just prints it. We bypass `console.print`'s rich-markup parsing because
    the summary contains backticks and brackets that Rich would otherwise
    interpret as styling tokens.
    """
    text = summarize_ledger(active_scope=active_scope)
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
