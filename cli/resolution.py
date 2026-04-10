"""Scope resolution lifecycle: resolve, object, withdraw, reopen.

Implements the resolution entry type and supporting objection / withdrawal /
reopen entry types per fnd-ledger.md -> Scope Resolution.

Resolution validation rules (Infrastructure Mode enforces before accepting):
  1. No open conflict breakers for the scope.
  2. No active objections for the scope.
  3. At least one verdict exists for the scope.
"""

from __future__ import annotations

from datetime import datetime, timezone

from rich.panel import Panel

from cli.schema import LedgerEntry
from cli.config import console, get_repo
from cli.ledger import (
    next_entry_id,
    write_entry,
    load_entry,
    entries_for_scope,
)


# ---------------------------------------------------------------------------
# Validation helpers — operate on a list of entries for a scope
# ---------------------------------------------------------------------------


def scope_is_resolved(entries: list[LedgerEntry]) -> tuple[bool, LedgerEntry | None]:
    """Determine whether a scope is currently resolved.

    A scope is resolved when a `resolution` entry exists and no subsequent
    `reopen` entry references it. Returns (is_resolved, resolution_entry).
    """
    # Walk entries forward; track the latest unreopened resolution
    resolution: LedgerEntry | None = None
    reopened_ids: set[str] = set()

    for e in entries:
        if e.type == "reopen":
            for ref in e.prior_entries:
                reopened_ids.add(ref)
        if e.type == "resolution":
            resolution = e

    if resolution is not None and resolution.entry_id not in reopened_ids:
        return True, resolution
    return False, None


def active_objections_for_scope(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Return objections that are still active (not withdrawn, not addressed by repair).

    An objection is active until:
      - The same author writes a `withdrawal` entry referencing it, OR
      - A completed `repair` entry references the objection as addressed.
    """
    objections = {e.entry_id: e for e in entries if e.type == "objection"}
    cleared: set[str] = set()

    for e in entries:
        if e.type == "withdrawal":
            for ref in e.prior_entries:
                if ref in objections:
                    # Only the original author can withdraw
                    if e.author == objections[ref].author:
                        cleared.add(ref)
        if e.type == "repair":
            for ref in e.prior_entries:
                if ref in objections:
                    cleared.add(ref)

    return [objections[oid] for oid in objections if oid not in cleared]


def open_conflict_breakers_for_scope(entries: list[LedgerEntry]) -> list[LedgerEntry]:
    """Return conflict failure entries that have no repair linking back.

    A conflict breaker is an open blocker if its failure entry exists but no
    repair entry references it via prior_entries.
    """
    failures = {
        e.entry_id: e for e in entries
        if e.type == "failure"
    }
    repaired: set[str] = set()
    for e in entries:
        if e.type == "repair":
            for ref in e.prior_entries:
                if ref in failures:
                    repaired.add(ref)

    return [failures[fid] for fid in failures if fid not in repaired]


def has_verdict_for_scope(entries: list[LedgerEntry]) -> bool:
    """Check if at least one completion entry with a verdict exists."""
    return any(
        e.type == "completion" and e.verdict is not None
        for e in entries
    )


def validate_resolution(entries: list[LedgerEntry]) -> list[str]:
    """Validate that a resolution can be written for this scope.

    Returns a list of blocking reasons. Empty list = validation passes.
    """
    blockers: list[str] = []

    # 1. No open conflict breakers
    open_conflicts = open_conflict_breakers_for_scope(entries)
    for f in open_conflicts:
        blockers.append(
            f"Open conflict breaker: failure entry {f.entry_id} "
            f"({f.summary[:80]}...) has no repair linking back."
        )

    # 2. No active objections
    active = active_objections_for_scope(entries)
    for obj in active:
        blockers.append(
            f"Active objection: entry {obj.entry_id} by {obj.author} "
            f"({obj.summary[:80]}...)."
        )

    # 3. At least one verdict exists
    if not has_verdict_for_scope(entries):
        blockers.append(
            "No verdict exists for this scope. An empty scope cannot be resolved."
        )

    return blockers


# ---------------------------------------------------------------------------
# CLI command functions
# ---------------------------------------------------------------------------


def cmd_resolve(scope: str, as_participant: str, references: list[str], summary: str) -> int:
    """Propose resolution for a scope."""
    entries = entries_for_scope(scope)

    # Check scope is not already resolved
    is_resolved, existing = scope_is_resolved(entries)
    if is_resolved:
        console.print(
            Panel(
                f"[bold red]Scope is already resolved.[/]\n\n"
                f"Resolution entry: {existing.entry_id}\n"
                f"Summary: {existing.summary}\n\n"
                f"To reopen, use:\n"
                f"  [cyan]orchestrator reopen --scope {scope} --as {as_participant} "
                f"--references {existing.entry_id} --reason \"...\"[/]",
                title="already resolved",
                border_style="red",
            )
        )
        return 2

    # Validate resolution
    blockers = validate_resolution(entries)
    if blockers:
        console.print(
            Panel(
                "[bold red]Resolution blocked.[/]\n\n"
                + "\n".join(f"  - {b}" for b in blockers)
                + "\n\nAddress the blockers above before proposing resolution.",
                title="validation failed",
                border_style="red",
            )
        )
        return 1

    # Validate references exist
    entry_ids = {e.entry_id for e in entries}
    bad_refs = [r for r in references if r not in entry_ids]
    if bad_refs:
        console.print(
            f"[red]error:[/] referenced entry IDs not found in scope: "
            f"{', '.join(bad_refs)}"
        )
        return 2

    repo = get_repo()
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=as_participant,
        type="resolution",
        scope=scope,
        prior_entries=references,
        summary=summary,
        detail="",
        confidence=1.0,
        foundation_tag=["truth", "boundaries"],
    )
    path = write_entry(entry, repo)
    console.print(
        Panel(
            f"[bold green]Resolution written.[/]\n\n"
            f"Entry: {entry.entry_id}\n"
            f"Scope: {scope}\n"
            f"References: {', '.join(references) if references else '(none)'}\n\n"
            f"{summary}",
            title=f"resolution {entry.entry_id}",
            border_style="green",
        )
    )
    return 0


def cmd_object(scope: str, as_participant: str, references: list[str], reason: str) -> int:
    """Raise an objection on a scope."""
    entries = entries_for_scope(scope)

    # Check scope is not resolved (objections only make sense on active scopes)
    is_resolved, _ = scope_is_resolved(entries)
    if is_resolved:
        console.print(
            f"[red]error:[/] scope is resolved. Use `reopen` first to return "
            f"it to active status before raising an objection."
        )
        return 2

    # Validate references exist
    entry_ids = {e.entry_id for e in entries}
    bad_refs = [r for r in references if r not in entry_ids]
    if bad_refs:
        console.print(
            f"[red]error:[/] referenced entry IDs not found in scope: "
            f"{', '.join(bad_refs)}"
        )
        return 2

    repo = get_repo()
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=as_participant,
        type="objection",
        scope=scope,
        prior_entries=references,
        summary=reason,
        detail="",
        confidence=1.0,
        foundation_tag=["truth"],
    )
    path = write_entry(entry, repo)
    console.print(
        Panel(
            f"[bold yellow]Objection raised.[/]\n\n"
            f"Entry: {entry.entry_id}\n"
            f"Scope: {scope}\n"
            f"By: {as_participant}\n"
            f"References: {', '.join(references) if references else '(none)'}\n\n"
            f"{reason}\n\n"
            f"This objection blocks resolution of the scope until withdrawn "
            f"or addressed through a repair cycle.",
            title=f"objection {entry.entry_id}",
            border_style="yellow",
        )
    )
    return 0


def cmd_withdraw_objection(scope: str, as_participant: str, references: list[str], reason: str) -> int:
    """Withdraw a prior objection."""
    entries = entries_for_scope(scope)

    # Validate each reference is an objection by this author
    objections = {e.entry_id: e for e in entries if e.type == "objection"}
    for ref in references:
        if ref not in objections:
            console.print(
                f"[red]error:[/] entry {ref} is not an objection entry in this scope."
            )
            return 2
        if objections[ref].author != as_participant:
            console.print(
                f"[red]error:[/] objection {ref} was authored by "
                f"{objections[ref].author}, not {as_participant}. "
                f"Only the original author can withdraw an objection."
            )
            return 2

    repo = get_repo()
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=as_participant,
        type="withdrawal",
        scope=scope,
        prior_entries=references,
        summary=reason or f"Withdrawing objection(s) {', '.join(references)}.",
        detail="",
        confidence=1.0,
        foundation_tag=["truth"],
    )
    path = write_entry(entry, repo)
    console.print(
        Panel(
            f"[bold cyan]Objection withdrawn.[/]\n\n"
            f"Entry: {entry.entry_id}\n"
            f"Withdrew: {', '.join(references)}\n"
            f"By: {as_participant}\n\n"
            f"{entry.summary}",
            title=f"withdrawal {entry.entry_id}",
            border_style="cyan",
        )
    )
    return 0


def cmd_reopen(scope: str, as_participant: str, references: list[str], reason: str) -> int:
    """Reopen a previously resolved scope."""
    entries = entries_for_scope(scope)

    is_resolved, resolution = scope_is_resolved(entries)
    if not is_resolved:
        console.print(
            f"[red]error:[/] scope is not currently resolved. "
            f"Nothing to reopen."
        )
        return 2

    # If no explicit references given, default to the current resolution entry
    if not references:
        references = [resolution.entry_id]

    # Validate references point to resolution entries
    resolutions = {e.entry_id for e in entries if e.type == "resolution"}
    bad_refs = [r for r in references if r not in resolutions]
    if bad_refs:
        console.print(
            f"[red]error:[/] referenced entries are not resolution entries: "
            f"{', '.join(bad_refs)}"
        )
        return 2

    repo = get_repo()
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=as_participant,
        type="reopen",
        scope=scope,
        prior_entries=references,
        summary=reason,
        detail="",
        confidence=1.0,
        foundation_tag=["truth", "intention"],
    )
    path = write_entry(entry, repo)
    console.print(
        Panel(
            f"[bold]Scope reopened.[/]\n\n"
            f"Entry: {entry.entry_id}\n"
            f"Scope: {scope}\n"
            f"Reopened resolution: {', '.join(references)}\n\n"
            f"{reason}\n\n"
            f"The scope is now active. New verdicts, objections, and "
            f"eventually a new resolution can follow.",
            title=f"reopen {entry.entry_id}",
            border_style="blue",
        )
    )
    return 0
