#!/usr/bin/env python3
"""
Coordination Daemon

Infrastructure Mode enacted as a service. This daemon:
1. Owns the ledger — accepts append proposals, validates schema, detects conflicts.
2. Monitors circuit breakers — timeout, conflict, resource, confidence, repetition.
3. Dispatches events — triggers Hermes sessions when the ledger state warrants them.
4. Tracks participant resource state — maintains Balance without LLM context cost.

This is NOT an LLM. It does not reason. It enforces schema, fires breakers, and dispatches.
All reasoning happens in Hermes sessions that the daemon triggers.

Usage:
    python daemon.py --config coordination.yaml
    python daemon.py --ledger ./ledger.jsonl --registry ./participants.yaml --port 8420
"""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import random
import string
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None

try:
    from aiohttp import web
except ImportError:
    web = None

try:
    import jsonschema
except ImportError:
    jsonschema = None

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "ledger_path": "./ledger.jsonl",
    "registry_path": "./participants.yaml",
    "schema_dir": "./schemas",
    "port": 8420,
    "watch_interval_seconds": 2,
    # Circuit breaker thresholds (from fnd-preamble.md)
    "breakers": {
        "resource_multiplier": 2.0,       # fires at 2x per-participant average
        "repetition_threshold": 3,         # 3+ attempts same scope without resolution
        "confidence_floor": 0.3,           # fires when confidence < 0.3 with no fallback
    },
    # How to dispatch Hermes sessions
    "hermes": {
        "command": "hermes",               # CLI command
        "dispatch_enabled": True,
    },
}


def load_config(path: Optional[str]) -> dict:
    config = dict(DEFAULT_CONFIG)
    if path and yaml and Path(path).exists():
        with open(path) as f:
            override = yaml.safe_load(f) or {}
        # shallow merge
        for k, v in override.items():
            if isinstance(v, dict) and k in config and isinstance(config[k], dict):
                config[k].update(v)
            else:
                config[k] = v
    return config


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------

class Ledger:
    """Append-only JSONL ledger with schema validation and conflict detection."""

    def __init__(self, path: str, schema_dir: str):
        self.path = Path(path)
        self.schema_dir = Path(schema_dir)
        self.entries: list[dict] = []
        self.entry_ids: set[str] = set()
        self.scope_owners: dict[str, str] = {}           # scope -> current owner
        self.pending_scopes: dict[str, list[dict]] = {}   # scope -> pending proposals
        self.resource_totals: dict[str, dict] = defaultdict(
            lambda: {"tokens": 0, "cost_usd": 0.0, "tasks": 0}
        )
        self._schema = None
        self._load_schema()
        self._load_existing()

    def _load_schema(self):
        schema_path = self.schema_dir / "ledger-entry.schema.json"
        if schema_path.exists() and jsonschema:
            with open(schema_path) as f:
                self._schema = json.load(f)

    def _load_existing(self):
        if self.path.exists():
            with open(self.path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        self.entries.append(entry)
                        self.entry_ids.add(entry["entry_id"])
                        self._index_entry(entry)
                    except (json.JSONDecodeError, KeyError) as e:
                        logging.warning(f"Skipping malformed ledger line: {e}")

    def _index_entry(self, entry: dict):
        """Update internal indices from an entry."""
        etype = entry.get("type")
        scope = entry.get("scope", "")
        author = entry.get("author", "")

        # Track resource snapshots
        rs = entry.get("resource_snapshot")
        if rs:
            totals = self.resource_totals[author]
            totals["tokens"] += rs.get("tokens_used", 0)
            totals["cost_usd"] += rs.get("cost_usd", 0.0)
            totals["tasks"] += 1

        # Track scope ownership
        if etype == "attempt":
            self.scope_owners[scope] = author
        elif etype == "completion":
            self.scope_owners.pop(scope, None)
        elif etype == "boundary_change":
            # Check if scope is being relinquished
            summary = entry.get("summary", "").lower()
            if "relinquish" in summary or "unowned" in summary or "departed" in summary:
                self.scope_owners.pop(scope, None)

    def validate(self, entry: dict) -> list[str]:
        """Validate an entry against the schema. Returns list of errors."""
        errors = []
        # Schema validation
        if self._schema and jsonschema:
            try:
                jsonschema.validate(entry, self._schema)
            except jsonschema.ValidationError as e:
                errors.append(f"Schema: {e.message}")

        # Referential integrity: prior_entries must exist
        for ref in entry.get("prior_entries", []):
            if ref not in self.entry_ids:
                errors.append(f"prior_entry '{ref}' does not exist in ledger")

        # entry_id must be unique
        if entry.get("entry_id") in self.entry_ids:
            errors.append(f"entry_id '{entry['entry_id']}' already exists")

        return errors

    def detect_conflict(self, entry: dict) -> Optional[dict]:
        """Check if this entry conflicts with a pending or recent entry on the same scope."""
        scope = entry.get("scope", "")
        etype = entry.get("type", "")
        author = entry.get("author", "")

        # Conflict: two state_update-like entries on same scope from different authors
        # within a short window, without one being an acknowledgment of the other
        if etype in ("attempt", "completion", "decision") and scope:
            # Check recent entries (last 60 seconds) for same scope, different author
            now = time.time()
            for existing in reversed(self.entries[-20:]):
                if existing.get("scope") != scope:
                    continue
                if existing.get("author") == author:
                    continue
                if existing.get("type") not in ("attempt", "completion", "decision"):
                    continue
                # Check if the new entry references the existing one (not a conflict)
                if existing["entry_id"] in entry.get("prior_entries", []):
                    continue
                try:
                    existing_time = datetime.fromisoformat(existing["timestamp"]).timestamp()
                    if now - existing_time < 60:
                        return existing
                except (ValueError, KeyError):
                    pass

        return None

    def append(self, entry: dict) -> dict:
        """Append a validated entry. Returns {"ok": True} or {"error": ...}."""
        errors = self.validate(entry)
        if errors:
            return {"ok": False, "errors": errors}

        conflict = self.detect_conflict(entry)
        if conflict:
            return {
                "ok": False,
                "conflict": True,
                "conflicting_entry": conflict["entry_id"],
                "errors": [
                    f"Conflict on scope '{entry['scope']}': "
                    f"entry {conflict['entry_id']} by {conflict['author']} "
                    f"touches the same scope within the conflict window."
                ]
            }

        # Append to file
        with open(self.path, "a") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")

        self.entries.append(entry)
        self.entry_ids.add(entry["entry_id"])
        self._index_entry(entry)
        return {"ok": True, "entry_id": entry["entry_id"]}

    def query(self, scope: Optional[str] = None, entry_type: Optional[str] = None,
              author: Optional[str] = None, last_n: Optional[int] = None) -> list[dict]:
        """Scoped read — filter entries."""
        results = self.entries
        if scope:
            results = [e for e in results if e.get("scope") == scope]
        if entry_type:
            results = [e for e in results if e.get("type") == entry_type]
        if author:
            results = [e for e in results if e.get("author") == author]
        if last_n:
            results = results[-last_n:]
        return results

    def summary(self, max_tokens_approx: int = 4000) -> list[dict]:
        """Generate a ledger summary per fnd-ledger.md read protocol.

        - Include summary field of every entry, omit detail.
        - Preserve failure, repair, intention_shift, boundary_change in full.
        - Compress decision/attempt/completion to summary-only for closed scopes.
        """
        PRESERVE_FULL = {"failure", "repair", "intention_shift", "boundary_change"}
        active_scopes = set(self.scope_owners.keys())
        summaries = []
        approx_chars = 0
        char_budget = max_tokens_approx * 4  # rough chars-per-token

        for entry in self.entries:
            etype = entry.get("type", "")
            scope = entry.get("scope", "")

            if etype in PRESERVE_FULL:
                compressed = {k: v for k, v in entry.items() if k != "detail"}
                summaries.append(compressed)
                approx_chars += len(json.dumps(compressed))
            elif scope in active_scopes:
                compressed = {k: v for k, v in entry.items() if k != "detail"}
                summaries.append(compressed)
                approx_chars += len(json.dumps(compressed))
            else:
                mini = {
                    "entry_id": entry["entry_id"],
                    "type": etype,
                    "scope": scope,
                    "summary": entry.get("summary", ""),
                    "author": entry.get("author", ""),
                }
                summaries.append(mini)
                approx_chars += len(json.dumps(mini))

            if approx_chars > char_budget:
                break

        return summaries


# ---------------------------------------------------------------------------
# Circuit Breakers
# ---------------------------------------------------------------------------

class CircuitBreakers:
    """Monitors ledger state for foundation breaches."""

    def __init__(self, ledger: Ledger, config: dict):
        self.ledger = ledger
        self.thresholds = config.get("breakers", DEFAULT_CONFIG["breakers"])
        self.fired: list[dict] = []  # breakers fired since last check

    def check_all(self) -> list[dict]:
        """Run all breaker checks. Returns list of breaches found."""
        self.fired = []
        self._check_repetition()
        self._check_resource()
        self._check_confidence()
        return self.fired

    def _check_repetition(self):
        """3+ attempts on same scope without completion, failure, or repair."""
        threshold = self.thresholds["repetition_threshold"]
        scope_attempts: dict[str, int] = defaultdict(int)

        for entry in self.ledger.entries:
            scope = entry.get("scope", "")
            etype = entry.get("type", "")
            if etype == "attempt":
                scope_attempts[scope] += 1
            elif etype in ("completion", "failure", "repair"):
                scope_attempts[scope] = 0

        for scope, count in scope_attempts.items():
            if count >= threshold:
                self.fired.append({
                    "breaker": "repetition",
                    "scope": scope,
                    "count": count,
                    "foundation_tag": ["recursion"],
                    "message": (
                        f"Scope '{scope}' has {count} attempt(s) without "
                        f"completion, failure, or repair."
                    ),
                })

    def _check_resource(self):
        """Any participant exceeding 2x the per-participant average."""
        multiplier = self.thresholds["resource_multiplier"]
        totals = self.ledger.resource_totals
        if not totals:
            return

        avg_cost = sum(t["cost_usd"] for t in totals.values()) / len(totals)
        avg_tokens = sum(t["tokens"] for t in totals.values()) / len(totals)

        for participant, t in totals.items():
            if avg_cost > 0 and t["cost_usd"] > avg_cost * multiplier:
                self.fired.append({
                    "breaker": "resource",
                    "participant": participant,
                    "metric": "cost_usd",
                    "value": t["cost_usd"],
                    "average": avg_cost,
                    "foundation_tag": ["balance"],
                    "message": (
                        f"Participant '{participant}' cost ${t['cost_usd']:.4f} "
                        f"exceeds {multiplier}x average ${avg_cost:.4f}."
                    ),
                })
            if avg_tokens > 0 and t["tokens"] > avg_tokens * multiplier:
                self.fired.append({
                    "breaker": "resource",
                    "participant": participant,
                    "metric": "tokens",
                    "value": t["tokens"],
                    "average": avg_tokens,
                    "foundation_tag": ["balance"],
                    "message": (
                        f"Participant '{participant}' used {t['tokens']} tokens, "
                        f"exceeds {multiplier}x average {avg_tokens:.0f}."
                    ),
                })

    def _check_confidence(self):
        """Recent entries with confidence < floor on scopes without fallback."""
        floor = self.thresholds["confidence_floor"]
        # Check last 10 entries
        for entry in self.ledger.entries[-10:]:
            conf = entry.get("confidence", 1.0)
            if conf < floor and entry.get("type") in ("attempt", "completion"):
                self.fired.append({
                    "breaker": "confidence",
                    "entry_id": entry["entry_id"],
                    "scope": entry.get("scope", ""),
                    "confidence": conf,
                    "foundation_tag": ["truth"],
                    "message": (
                        f"Entry {entry['entry_id']} on scope '{entry.get('scope', '')}' "
                        f"reports confidence {conf} (below floor {floor})."
                    ),
                })


# ---------------------------------------------------------------------------
# Event Dispatcher
# ---------------------------------------------------------------------------

class Dispatcher:
    """Triggers Hermes sessions in response to ledger events."""

    def __init__(self, config: dict, registry: dict):
        self.config = config.get("hermes", {})
        self.registry = registry
        self.enabled = self.config.get("dispatch_enabled", True)

    def dispatch_repair(self, breach: dict, ledger_summary: list[dict]):
        """Trigger a Hermes session for the repair cycle."""
        if not self.enabled:
            logging.info(f"[dispatch] repair needed but dispatch disabled: {breach}")
            return

        prompt = self._build_repair_prompt(breach, ledger_summary)
        self._run_hermes(prompt)

    def dispatch_unowned_scope(self, scope: str, ledger_summary: list[dict]):
        """Trigger a Hermes session to pick up unowned scope."""
        if not self.enabled:
            logging.info(f"[dispatch] unowned scope '{scope}' but dispatch disabled")
            return

        prompt = self._build_scope_prompt(scope, ledger_summary)
        self._run_hermes(prompt)

    def _build_repair_prompt(self, breach: dict, summary: list[dict]) -> str:
        return (
            f"A circuit breaker has fired in the coordination.\n\n"
            f"Breach: {json.dumps(breach, indent=2)}\n\n"
            f"Load fnd-repair.md and enter the repair cycle. "
            f"The ledger summary is:\n\n"
            f"```json\n{json.dumps(summary[-20:], indent=2)}\n```\n\n"
            f"Diagnose the breach, propose a resolution, and write a repair entry "
            f"to the ledger via the coordination daemon API at "
            f"http://localhost:{self.config.get('port', 8420)}/append"
        )

    def _build_scope_prompt(self, scope: str, summary: list[dict]) -> str:
        return (
            f"Unowned scope detected in the coordination: '{scope}'\n\n"
            f"Review the ledger summary and determine if you can contribute. "
            f"If yes, write an attempt entry. If not, write a decision entry "
            f"noting the scope remains available.\n\n"
            f"Ledger summary:\n```json\n{json.dumps(summary[-20:], indent=2)}\n```\n\n"
            f"Daemon API: http://localhost:{self.config.get('port', 8420)}/append"
        )

    def _run_hermes(self, prompt: str):
        """Run a Hermes CLI session with the given prompt."""
        cmd = self.config.get("command", "hermes")
        try:
            logging.info(f"[dispatch] triggering hermes session")
            subprocess.Popen(
                [cmd, "--prompt", prompt],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            logging.error(f"[dispatch] hermes command '{cmd}' not found")
        except Exception as e:
            logging.error(f"[dispatch] failed to trigger hermes: {e}")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def load_registry(path: str) -> dict:
    """Load participant registry from YAML."""
    if not yaml:
        logging.warning("PyYAML not installed; registry unavailable")
        return {"participants": []}
    p = Path(path)
    if not p.exists():
        logging.warning(f"Registry not found at {path}")
        return {"participants": []}
    with open(p) as f:
        return yaml.safe_load(f) or {"participants": []}


def select_participant(registry: dict, capability: str, max_cost: float = None) -> Optional[dict]:
    """Route by capability and cost — Foundation IV (Balance)."""
    candidates = []
    for p in registry.get("participants", []):
        if p.get("participation_mode") != "active":
            continue
        if p.get("capacity") == "reduced":
            continue
        envelope = p.get("capability_envelope", {})
        score = envelope.get(capability, 0.0)
        if score > 0.0:
            cost_per_1k = (
                p.get("cost_model", {}).get("input_cost_per_1k", 0) +
                p.get("cost_model", {}).get("output_cost_per_1k", 0)
            )
            if max_cost is not None and cost_per_1k > max_cost:
                continue
            candidates.append((p, score, cost_per_1k))

    if not candidates:
        return None

    # Sort by capability score descending, then cost ascending
    candidates.sort(key=lambda x: (-x[1], x[2]))
    return candidates[0][0]


# ---------------------------------------------------------------------------
# Utility: generate entry_id
# ---------------------------------------------------------------------------

def make_entry_id(author: str) -> str:
    ts = int(time.time() * 1000)
    rand = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{author}-{ts}-{rand}"


# ---------------------------------------------------------------------------
# HTTP API
# ---------------------------------------------------------------------------

async def handle_append(request, ledger: Ledger, breakers: CircuitBreakers, dispatcher: Dispatcher):
    """POST /append — propose a ledger entry."""
    try:
        entry = await request.json()
    except Exception:
        return web.json_response({"ok": False, "errors": ["Invalid JSON"]}, status=400)

    result = ledger.append(entry)

    if result.get("ok"):
        # Check breakers after every successful append
        breaches = breakers.check_all()
        if breaches:
            for breach in breaches:
                # Auto-write failure entry for each breach
                failure_entry = {
                    "entry_id": make_entry_id("daemon"),
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "author": "daemon",
                    "type": "failure",
                    "scope": breach.get("scope", breach.get("participant", "coordination")),
                    "prior_entries": [entry["entry_id"]],
                    "summary": breach["message"],
                    "detail": json.dumps(breach),
                    "confidence": 1.0,
                    "foundation_tag": breach["foundation_tag"],
                }
                ledger.append(failure_entry)
                dispatcher.dispatch_repair(breach, ledger.summary())

            result["breakers_fired"] = breaches

    status = 200 if result.get("ok") else 409 if result.get("conflict") else 400
    return web.json_response(result, status=status)


async def handle_query(request, ledger: Ledger):
    """GET /query — scoped read."""
    scope = request.query.get("scope")
    entry_type = request.query.get("type")
    author = request.query.get("author")
    last_n = request.query.get("last_n")
    last_n = int(last_n) if last_n else None

    results = ledger.query(scope=scope, entry_type=entry_type, author=author, last_n=last_n)
    return web.json_response(results)


async def handle_summary(request, ledger: Ledger):
    """GET /summary — ledger summary for session bootstrapping."""
    max_tokens = int(request.query.get("max_tokens", 4000))
    return web.json_response(ledger.summary(max_tokens))


async def handle_registry(request, registry: dict):
    """GET /registry — current participant declarations."""
    return web.json_response(registry)


async def handle_select(request, registry: dict):
    """GET /select?capability=X&max_cost=Y — route by capability and cost."""
    capability = request.query.get("capability", "")
    max_cost = request.query.get("max_cost")
    max_cost = float(max_cost) if max_cost else None
    result = select_participant(registry, capability, max_cost)
    if result:
        return web.json_response({"found": True, "participant": result})
    return web.json_response({"found": False}, status=404)


async def handle_health(request, ledger: Ledger):
    """GET /health — daemon health and coordination stats."""
    return web.json_response({
        "status": "ok",
        "entries": len(ledger.entries),
        "active_scopes": list(ledger.scope_owners.keys()),
        "resource_totals": dict(ledger.resource_totals),
    })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build_app(config: dict) -> web.Application:
    ledger = Ledger(config["ledger_path"], config["schema_dir"])
    registry = load_registry(config["registry_path"])
    breakers = CircuitBreakers(ledger, config)
    dispatcher = Dispatcher(config, registry)

    app = web.Application()
    app.router.add_post("/append", lambda r: handle_append(r, ledger, breakers, dispatcher))
    app.router.add_get("/query", lambda r: handle_query(r, ledger))
    app.router.add_get("/summary", lambda r: handle_summary(r, ledger))
    app.router.add_get("/registry", lambda r: handle_registry(r, registry))
    app.router.add_get("/select", lambda r: handle_select(r, registry))
    app.router.add_get("/health", lambda r: handle_health(r, ledger))
    return app


def main():
    parser = argparse.ArgumentParser(description="Coordination Daemon")
    parser.add_argument("--config", help="Path to coordination.yaml")
    parser.add_argument("--ledger", help="Path to ledger JSONL file")
    parser.add_argument("--registry", help="Path to participants.yaml")
    parser.add_argument("--schema-dir", help="Path to schemas directory")
    parser.add_argument("--port", type=int, help="HTTP port")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.ledger:
        config["ledger_path"] = args.ledger
    if args.registry:
        config["registry_path"] = args.registry
    if args.schema_dir:
        config["schema_dir"] = args.schema_dir
    if args.port:
        config["port"] = args.port

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [daemon] %(levelname)s %(message)s",
    )

    if web is None:
        print("aiohttp is required: pip install aiohttp", file=sys.stderr)
        sys.exit(1)

    logging.info(f"Ledger: {config['ledger_path']}")
    logging.info(f"Registry: {config['registry_path']}")
    logging.info(f"Port: {config['port']}")

    app = build_app(config)
    web.run_app(app, port=config["port"])


if __name__ == "__main__":
    main()
