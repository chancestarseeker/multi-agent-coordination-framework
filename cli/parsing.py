"""JSON extraction and entry finalization from LLM responses."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone

from cli.schema import (
    LedgerEntry,
    VALID_ENTRY_TYPES,
    VALID_SIGNAL_TYPES,
)
from cli.ledger import next_entry_id


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
    # Author is set unconditionally — not setdefault. Per fnd-field.md:
    # "Write to the ledger on behalf of a participant without that
    # participant's signal" is forbidden. A malicious agent embedding
    # proposed_entry.author="human-lead" in a state_update signal
    # would forge entries attributed to the human if we used setdefault.
    raw["author"] = author
    raw.setdefault("scope", scope_path)
    raw.setdefault("prior_entries", [])
    raw.setdefault("foundation_tag", [])
    raw.setdefault("detail", "")
    return LedgerEntry(**raw)
