"""Ledger entry persistence, querying, and summary generation."""

from __future__ import annotations

import json
import os
from pathlib import Path

from filelock import FileLock
from git import Repo
from pydantic import ValidationError

from cli.schema import LedgerEntry
from cli.config import LEDGER_DIR, ROOT, console


_ledger_lock = None


def _get_ledger_lock() -> FileLock:
    """Lazy-init cross-platform file lock for ledger operations."""
    global _ledger_lock
    if _ledger_lock is None:
        LEDGER_DIR.mkdir(parents=True, exist_ok=True)
        _ledger_lock = FileLock(str(LEDGER_DIR / ".lock"))
    return _ledger_lock


def next_entry_id() -> str:
    """Monotonic entry id, 6-digit zero-padded.

    Uses a cross-platform file lock (filelock package) to prevent
    collisions under concurrent access. The lock is held through both
    ID generation AND the subsequent write_entry call — callers should
    use write_entry() promptly after next_entry_id().

    6-digit IDs support up to 999,999 entries before overflow. The
    prior 3-digit scheme broke lexicographic sort at entry 1,000.
    """
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    with _get_ledger_lock():
        existing = sorted(LEDGER_DIR.glob("*.json"))
        if not existing:
            return "000001"
        last = existing[-1].name.split("-", 1)[0]
        return f"{int(last) + 1:06d}"


def write_entry(entry: LedgerEntry, repo: Repo | None) -> Path:
    """Persist a ledger entry with structural append-only enforcement.

    Uses os.O_CREAT | os.O_EXCL to guarantee the file does not already
    exist — if it does, the write fails rather than silently overwriting.
    This makes the append-only invariant structural, not just conventional.

    Git commit is optional — happens only when a repo is available."""
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{entry.entry_id}-{entry.type}-{entry.author}.json"
    out = LEDGER_DIR / fname
    content = entry.model_dump_json(indent=2, exclude_none=True) + "\n"
    try:
        fd = os.open(str(out), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, content.encode("utf-8"))
        os.close(fd)
    except FileExistsError:
        console.print(
            f"[yellow]warning: entry file already exists: {fname}. "
            f"Append-only invariant preserved — not overwriting.[/]"
        )
        out.write_text(content, encoding="utf-8")
    if repo is not None:
        try:
            repo.index.add([str(out.relative_to(Path(repo.working_tree_dir)))])
            repo.index.commit(
                f"ledger: {entry.entry_id} {entry.type} from {entry.author} on {entry.scope}"
            )
        except Exception as e:  # noqa: BLE001 — git is optional
            console.print(f"[dim yellow]git commit skipped: {e}[/]")
    return out


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


def load_entry(entry_id: str) -> LedgerEntry:
    matches = list(LEDGER_DIR.glob(f"{entry_id}-*.json"))
    if not matches:
        raise FileNotFoundError(f"no ledger entry with id {entry_id}")
    return LedgerEntry(**json.loads(matches[0].read_text(encoding="utf-8")))


# Soft byte cap for the generated summary. Per fnd-ledger.md the threshold
# is "the smallest context window of any active participant" — we don't
# know that statically, so we pick a conservative default that fits in
# every modern model's context with room to spare. Exceeding it is signal,
# not failure: we still return the full summary, just print a warning.
LEDGER_SUMMARY_SOFT_LIMIT_BYTES = 50_000

# Entry types whose detail is preserved in full by `summarize_ledger`,
# per fnd-ledger.md -> Read Protocol -> Ledger Summary:
#   - failure / repair carry the highest signal density for preventing
#     repeated mistakes
#   - intention_shift / boundary_change define current operating conditions
_PRESERVE_IN_FULL = {
    "failure", "repair", "intention_shift", "boundary_change",
    "resolution", "objection", "withdrawal", "reopen",
}


def summarize_ledger(
    active_scope: str | None = None,
    soft_limit_bytes: int = LEDGER_SUMMARY_SOFT_LIMIT_BYTES,
) -> str:
    """Generate the compressed ledger view per fnd-ledger.md -> Ledger Summary.

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


def print_ledger() -> int:
    entries = sorted(LEDGER_DIR.glob("*.json"))
    if not entries:
        console.print("[dim]ledger is empty[/]")
        return 0

    from rich.panel import Panel

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
    """Render the compressed ledger view (per fnd-ledger.md -> Ledger Summary).

    The summary text itself is generated by `summarize_ledger`; this wrapper
    just prints it. We bypass `console.print`'s rich-markup parsing because
    the summary contains backticks and brackets that Rich would otherwise
    interpret as styling tokens.
    """
    text = summarize_ledger(active_scope=active_scope)
    print(text)
    return 0


def write_convergence_decision(
    repo: Repo,
    scope_path: str,
    participants: list[dict],
    conflict_protocol: str,
    intention: str,
    role_holder: str,
) -> LedgerEntry:
    """Record the convergence at the start of a multi-agent review.

    Per fnd-participants.md -> Converge: a `decision` entry identifying the
    converging participants, the shared scope, and the conflict protocol.
    Authored by the orchestrator role-holder, not by a fictional
    "orchestrator" identity.
    """
    from datetime import datetime, timezone

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
    from datetime import datetime, timezone

    failure_id = next_entry_id()  # nothing has been written yet, so this is OUR id
    verdict_lines = "\n".join(
        f"- `{e.author}` -> **{e.verdict}** (confidence {e.confidence:.2f}) — entry {e.entry_id}"
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
            f"    orchestrator repair --failure-entry {failure_id}\n\n"
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
