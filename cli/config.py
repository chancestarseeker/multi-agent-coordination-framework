"""Configuration loading, path constants, and provider routing.

Imports from schema.py only.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from git import Repo
from git.exc import InvalidGitRepositoryError, NoSuchPathError
from rich.console import Console

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


# ---------- Loading ----------

_cached_config: dict | None = None


def load_config() -> dict:
    """Load config.json, cached for the lifetime of this process.

    The config is read once from disk and reused on subsequent calls.
    This avoids redundant file reads (the old version re-read on every
    call, including inside the retry loop). The cache is process-scoped,
    so a new CLI invocation always reads fresh config.
    """
    global _cached_config
    if _cached_config is None:
        _cached_config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return _cached_config


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


def resolve_scope(scope_rel: str) -> Path:
    """Resolve a scope path relative to ROOT, with path-traversal protection.

    Prevents directory traversal attacks (e.g., --scope ../../etc/passwd)
    by verifying the resolved path stays within ROOT. Raises ValueError
    if the path escapes the coordination directory.
    """
    scope_abs = (ROOT / scope_rel).resolve()
    if not scope_abs.is_relative_to(ROOT.resolve()):
        raise ValueError(
            f"scope path escapes the coordination directory: "
            f"{scope_rel!r} resolves to {scope_abs}, which is outside {ROOT.resolve()}"
        )
    return scope_abs
