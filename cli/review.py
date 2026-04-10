"""Review loop: run_review, route_participants, infer_task_type, print_summary."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.table import Table

from cli.schema import LedgerEntry
from cli.config import (
    console,
    load_config,
    load_declarations,
    load_foundations,
    resolve_scope,
    get_repo,
)
from cli.prompts import build_messages
from cli.ledger import (
    write_entry,
    entries_for_scope,
    write_convergence_decision,
    write_conflict_failure,
    unresolved_failures_for_scope,
)
from cli.retry import request_entry_with_retry, write_participant_failure
from cli.breakers import (
    detect_verdict_conflict,
    repetition_breaker_should_fire,
    write_repetition_failure,
    check_resource_breaker,
    check_resource_ceiling,
)
from cli.roles import current_orchestrator_for_scope


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


def run_review(scope_rel: str, task_type: str | None = None) -> int:
    config = load_config()
    declarations = load_declarations()
    foundations_text = load_foundations(config.get("foundations_loaded_by_default", []))
    intention = config.get("intention", "")
    convergence_cfg = config.get("convergence", {})
    conflict_protocol = convergence_cfg.get("default_protocol", "escalate_to_repair")

    try:
        scope_abs = resolve_scope(scope_rel)
    except ValueError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
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
        console.print(f"\n[cyan]-> requesting review from {author} ({model})...[/]")

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
            console.print(f"  [red]x {author} could not produce a valid entry:[/] {error}")
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
                f"  [green]v[/] wrote {path.name} "
                f"(verdict={entry.verdict or '—'}, confidence={entry.confidence:.2f})"
            )
        elif entry.type == "failure":
            console.print(
                f"  [yellow]o[/] {author} refused: {entry.summary}"
            )
        if entry.confidence < config["circuit_breakers"]["confidence_floor"]:
            console.print(
                f"  [yellow]! confidence breaker would fire (< "
                f"{config['circuit_breakers']['confidence_floor']})[/]"
            )
        results.append({
            "author": author,
            "entry_id": entry.entry_id,
            "type": entry.type,
            "verdict": entry.verdict,
            "confidence": entry.confidence,
            "summary": entry.summary,
            "path": str(path.relative_to(Path(__file__).resolve().parent)),
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
