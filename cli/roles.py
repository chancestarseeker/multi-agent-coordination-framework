"""Orchestrator role lifecycle: offer, accept, refuse, rotate, stepdown, self-select.

The role is offered by the field and accepted by a participant, not taken.
Departure from the role is triggered by field conditions (rotation threshold,
breaker, mode transition), not solely by the holder's choice. Per fnd-field.md:
'The role is recognized, not owned.'
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from rich.panel import Panel

from cli.schema import LedgerEntry
from cli.config import ROOT, console, load_config, load_declarations
from cli.ledger import (
    next_entry_id,
    write_entry,
    entries_for_scope,
    load_entry,
)


# ---------- State machine ----------


def current_orchestrator_for_scope(scope_rel: str) -> str | None:
    """Walk the ledger for the current role holder.

    The state machine: an accept_orchestrator entry begins a holding period;
    a rotate_orchestrator or stepdown_orchestrator entry ends it. An
    offer_orchestrator entry does NOT grant the role — only acceptance does.
    """
    entries = entries_for_scope(scope_rel)
    holder: str | None = None
    for e in entries:
        if e.role_action == "accept_orchestrator":
            holder = e.author
        elif e.role_action in ("rotate_orchestrator", "stepdown_orchestrator") and e.author == holder:
            holder = None
    return holder


# Structured tag used to store the offered-to participant in the offer entry's
# summary field, so we don't have to parse markdown. Format: [offered:identifier]
_OFFERED_TAG_PREFIX = "[offered:"
_OFFERED_TAG_SUFFIX = "]"


def pending_offer_for_scope(scope_rel: str) -> tuple[LedgerEntry | None, str | None]:
    """Find the most recent unresolved, non-expired offer for a scope.

    An offer is 'unresolved' if there is no subsequent accept, refuse, or
    rotate entry referencing it. An offer is 'expired' if it is older than
    the offered participant's latency_tolerance_seconds (or a default of
    3600s if the participant has no declaration or no tolerance declared).

    Returns (offer_entry, offered_to_participant) or (None, None).
    """
    entries = entries_for_scope(scope_rel)
    resolved_offer_ids: set[str] = set()
    for e in entries:
        if e.role_action in ("accept_orchestrator", "refuse_orchestrator", "rotate_orchestrator"):
            for pid in e.prior_entries:
                resolved_offer_ids.add(pid)

    declarations = load_declarations()
    decl_map = {d["identifier"]: d for d in declarations}
    now = datetime.now(timezone.utc)

    # Walk backward to find the most recent unresolved offer
    for e in reversed(entries):
        if e.role_action == "offer_orchestrator" and e.entry_id not in resolved_offer_ids:
            offered_to = _extract_offered_to(e)

            # Check if the offer has expired
            if offered_to:
                decl = decl_map.get(offered_to)
                tolerance = 3600  # default 1 hour
                if decl:
                    tolerance = (decl.get("context_constraints") or {}).get(
                        "latency_tolerance_seconds", tolerance
                    )
                try:
                    offer_time = datetime.fromisoformat(e.timestamp.replace("Z", "+00:00"))
                    elapsed = (now - offer_time).total_seconds()
                    if elapsed > tolerance:
                        # Offer expired — treat as if no pending offer
                        return None, None
                except (ValueError, TypeError):
                    pass

            return e, offered_to
    return None, None


def _extract_offered_to(offer_entry: LedgerEntry) -> str | None:
    """Extract the offered-to participant from the structured tag in summary.

    Offer entries embed [offered:identifier] in the summary field for
    reliable extraction without markdown parsing.
    """
    summary = offer_entry.summary
    start = summary.find(_OFFERED_TAG_PREFIX)
    if start < 0:
        # Fallback: try markdown format for backward compat
        for line in offer_entry.detail.split("\n"):
            if line.startswith("**Offered to:**"):
                tick_start = line.find("`")
                tick_end = line.find("`", tick_start + 1)
                if tick_start >= 0 and tick_end > tick_start:
                    return line[tick_start + 1:tick_end]
        return None
    start += len(_OFFERED_TAG_PREFIX)
    end = summary.find(_OFFERED_TAG_SUFFIX, start)
    if end < 0:
        return None
    return summary[start:end]


def check_rotation_triggers(
    scope_rel: str,
    role_holder: str,
    config: dict,
) -> str | None:
    """Check if field conditions require rotating the role away from the holder.

    Returns a reason string if rotation should fire, None otherwise.

    Triggers:
      - Entry count: holder has authored more than max_entries_per_holder
        entries on this scope since accepting the role.
      - Time: holder has held the role for longer than max_seconds_per_holder.
    """
    rotation_cfg = config.get("role_rotation", {})
    max_entries = rotation_cfg.get("max_entries_per_holder")
    max_seconds = rotation_cfg.get("max_seconds_per_holder")

    if max_entries is None and max_seconds is None:
        return None

    entries = entries_for_scope(scope_rel)

    # Find when the holder accepted the role (most recent accept_orchestrator)
    accept_time: str | None = None
    entries_since_accept = 0
    counting = False
    for e in entries:
        if e.role_action == "accept_orchestrator" and e.author == role_holder:
            accept_time = e.timestamp
            entries_since_accept = 0
            counting = True
        elif counting and e.author == role_holder:
            entries_since_accept += 1

    # Entry count trigger
    if max_entries is not None and entries_since_accept >= max_entries:
        return (
            f"Rotation threshold reached: {role_holder} has authored "
            f"{entries_since_accept} entries on {scope_rel} since accepting "
            f"the role (threshold: {max_entries})."
        )

    # Time trigger
    if max_seconds is not None and accept_time is not None:
        try:
            accepted_at = datetime.fromisoformat(accept_time.replace("Z", "+00:00"))
            elapsed = (datetime.now(timezone.utc) - accepted_at).total_seconds()
            if elapsed > max_seconds:
                return (
                    f"Rotation threshold reached: {role_holder} has held the "
                    f"role on {scope_rel} for {elapsed:.0f}s "
                    f"(threshold: {max_seconds}s)."
                )
        except (ValueError, TypeError):
            pass

    return None


# ---------- Role lifecycle entries ----------


def write_role_offer(
    repo: Repo | None,
    scope_rel: str,
    offered_to: dict,
    reason: str,
    offered_by: str = "field",
) -> LedgerEntry:
    """The field proposes the orchestrator role to a participant.

    Per fnd-field.md: 'A participant accepts the orchestrator role for a
    declared scope.' The role is offered, not taken. The participant must
    accept via a separate accept entry.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=offered_by,
        type="decision",
        scope=scope_rel,
        prior_entries=[],
        summary=(
            f"Orchestrator role offered to `{offered_to['identifier']}` for "
            f"{scope_rel}. {reason} "
            f"{_OFFERED_TAG_PREFIX}{offered_to['identifier']}{_OFFERED_TAG_SUFFIX}"
        ),
        detail=(
            f"# Orchestrator role offer\n\n"
            f"**Offered to:** `{offered_to['identifier']}` "
            f"(steward: {offered_to.get('steward', '?')})\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Reason:** {reason}\n\n"
            f"Per fnd-field.md: 'The role is recognized, not owned.' This offer "
            f"proposes the role — the participant must accept or refuse. Until "
            f"accepted, no one holds the orchestrator role for this scope.\n\n"
            f"To accept:\n"
            f"    orchestrator accept-role --scope {scope_rel} "
            f"--as {offered_to['identifier']}\n\n"
            f"To refuse:\n"
            f"    orchestrator refuse-role --scope {scope_rel} "
            f"--as {offered_to['identifier']} --reason '...'"
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries"],
        role_action="offer_orchestrator",
    )
    write_entry(entry, repo)
    return entry


def write_role_acceptance(
    repo: Repo | None,
    scope_rel: str,
    participant: dict,
    offer_entry: LedgerEntry,
) -> LedgerEntry:
    """A participant accepts the offered orchestrator role.

    Per fnd-field.md: 'A participant accepts the orchestrator role for a
    declared scope. The role is recorded in the ledger and, when other
    participants are active in scope, acknowledged by at least one of them.'

    Per fnd-participants.md: 'Acceptance is recorded as an attempt entry.'
    However, for the role state machine, we use a decision entry with
    role_action=accept_orchestrator to keep the state machine consistent.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="decision",
        scope=scope_rel,
        prior_entries=[offer_entry.entry_id],
        summary=(
            f"`{participant['identifier']}` accepts the orchestrator role for "
            f"{scope_rel}. Routing decisions on this scope will be attributed "
            f"to this participant."
        ),
        detail=(
            f"# Orchestrator role accepted\n\n"
            f"**Participant:** `{participant['identifier']}` "
            f"(steward: {participant.get('steward', '?')})\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Accepting offer:** {offer_entry.entry_id}\n\n"
            f"Per fnd-field.md: 'The role is recognized, not owned: it can be "
            f"transferred, rotated, or released through the transition "
            f"safeguards.'\n\n"
            f"The orchestrator is a participant, not a supervisor. It has a "
            f"declaration. It has boundaries. It can be refused. It can be "
            f"replaced."
        ),
        confidence=1.0,
        foundation_tag=["boundaries", "intention"],
        role_action="accept_orchestrator",
    )
    write_entry(entry, repo)
    return entry


def write_role_refusal(
    repo: Repo | None,
    scope_rel: str,
    participant: dict,
    offer_entry: LedgerEntry,
    reason: str,
) -> LedgerEntry:
    """A participant refuses the offered orchestrator role."""
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="decision",
        scope=scope_rel,
        prior_entries=[offer_entry.entry_id],
        summary=(
            f"`{participant['identifier']}` refuses the orchestrator role for "
            f"{scope_rel}. Reason: {reason}"
        ),
        detail=(
            f"# Orchestrator role refused\n\n"
            f"**Participant:** `{participant['identifier']}`\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Refusing offer:** {offer_entry.entry_id}\n\n"
            f"**Reason:** {reason}\n\n"
            f"Per fnd-field.md and fnd-participants.md, refusal is signal, "
            f"not failure. The field should re-offer to the next candidate."
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries"],
        role_action="refuse_orchestrator",
    )
    write_entry(entry, repo)
    return entry


def write_role_rotation(
    repo: Repo | None,
    scope_rel: str,
    participant: dict,
    reason: str,
    snapshot: str | None = None,
    transferring_to: str | None = None,
) -> LedgerEntry:
    """Field-triggered release: rotation threshold, breaker, mode transition.

    This is NOT the holder's choice — it is the field responding to conditions.
    The holder's identity is used as author because they are the affected
    participant, but the trigger is external.
    """
    snapshot_section = ""
    if snapshot is not None:
        snapshot_section = f"\n\n## State Snapshot\n\n{snapshot}"

    transfer_note = ""
    if transferring_to:
        transfer_note = f"\n\n**Next candidate:** `{transferring_to}`"

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="decision",
        scope=scope_rel,
        prior_entries=[],
        summary=(
            f"Orchestrator role rotated from `{participant['identifier']}` on "
            f"{scope_rel}. Trigger: {reason}"
        ),
        detail=(
            f"# Orchestrator role rotated (field-triggered)\n\n"
            f"**Participant:** `{participant['identifier']}`\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Trigger:** {reason}\n\n"
            f"Per fnd-field.md: 'The role is recognized, not owned: it can be "
            f"transferred, rotated, or released through the transition "
            f"safeguards.' This rotation was triggered by field conditions, "
            f"not by the holder's choice."
            f"{transfer_note}{snapshot_section}"
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries", "balance"],
        role_action="rotate_orchestrator",
    )
    write_entry(entry, repo)
    return entry


def write_role_stepdown(
    repo: Repo | None,
    scope_rel: str,
    participant: dict,
    reason: str,
    snapshot: str | None = None,
) -> LedgerEntry:
    """Voluntary step-down, framed as a boundary_change.

    The holder recognizes they can no longer serve the scope and steps down.
    Per fnd-participants.md: 'Relinquish is an act of Choice — recognizing
    the boundary of what one can influence and releasing what one cannot.'
    """
    snapshot_section = ""
    if snapshot is not None:
        snapshot_section = f"\n\n## State Snapshot\n\n{snapshot}"

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=participant["identifier"],
        type="decision",
        scope=scope_rel,
        prior_entries=[],
        summary=(
            f"`{participant['identifier']}` steps down from orchestrator role "
            f"on {scope_rel}. Reason: {reason}"
        ),
        detail=(
            f"# Orchestrator role stepdown (voluntary)\n\n"
            f"**Participant:** `{participant['identifier']}`\n\n"
            f"**Scope:** `{scope_rel}`\n\n"
            f"**Reason:** {reason}\n\n"
            f"Per fnd-participants.md → Relinquish: 'A participant that holds "
            f"scope beyond its capacity to serve it is doing more harm than "
            f"one that lets go.' This stepdown is a boundary_change: the "
            f"participant's relationship to the scope has changed.\n\n"
            f"The field should now offer the role to the next best candidate."
            f"{snapshot_section}"
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries"],
        role_action="stepdown_orchestrator",
    )
    write_entry(entry, repo)
    return entry


# ---------- Candidate selection ----------


def select_role_candidate(
    declarations: list[dict],
    scope_rel: str,
    exclude: list[str] | None = None,
) -> dict | None:
    """Select the best candidate for the orchestrator role on a scope.

    Per fnd-field.md: 'Routing considers declaration match, boundaries,
    resource state, cost, and complementarity. When multiple participants
    are materially fit, prefer the routing choice that broadens stewardship,
    lineage, or failure profile.'

    Returns the best-fit declaration, or None if no candidates are available.
    Excludes participants in the `exclude` list (e.g., the current holder
    who is being rotated out).
    """
    exclude_set = set(exclude or [])
    candidates = [
        d for d in declarations
        if d.get("participation_mode") == "active"
        and d["identifier"] not in exclude_set
    ]

    if not candidates:
        return None

    # Score by: has litellm_model (agents preferred for automated routing),
    # then by broadest capability envelope, then by capacity == "full"
    def score(d: dict) -> tuple:
        has_model = 1 if d.get("litellm_model") else 0
        cap_breadth = len(d.get("capability_envelope", {}))
        full_cap = 1 if d.get("capacity") == "full" else 0
        return (has_model, cap_breadth, full_cap)

    candidates.sort(key=score, reverse=True)
    return candidates[0]


# ---------- CLI commands ----------


def cmd_offer_role(scope_rel: str, to_participant: str | None = None) -> int:
    """offer-role: the field proposes the role to the best candidate."""
    declarations = load_declarations()

    current = current_orchestrator_for_scope(scope_rel)
    if current is not None:
        console.print(
            Panel(
                f"[bold red]Cannot offer orchestrator role on {scope_rel}.[/]\n\n"
                f"`{current}` already holds the role. The role must be rotated "
                f"or the holder must step down before a new offer can be made.",
                title="role already held",
                border_style="red",
            )
        )
        return 1

    # Check for pending unresolved offer
    pending_offer, pending_to = pending_offer_for_scope(scope_rel)
    if pending_offer is not None:
        console.print(
            Panel(
                f"[bold yellow]Pending offer already exists.[/]\n\n"
                f"Entry {pending_offer.entry_id} offered the role to "
                f"`{pending_to or '?'}`. They must accept or refuse before "
                f"a new offer can be made.",
                title="pending offer",
                border_style="yellow",
            )
        )
        return 1

    if to_participant:
        candidate = next(
            (d for d in declarations if d["identifier"] == to_participant), None
        )
        if candidate is None:
            console.print(
                f"[red]error:[/] no declaration found for `{to_participant}`."
            )
            return 2
        reason = f"Explicitly offered to `{to_participant}` by the coordination."
    else:
        candidate = select_role_candidate(declarations, scope_rel)
        if candidate is None:
            console.print("[red]error:[/] no eligible candidates for the orchestrator role.")
            return 2
        reason = (
            f"Field selected `{candidate['identifier']}` as best candidate "
            f"based on declaration match, capacity, and capability breadth."
        )

    from cli.config import get_repo
    repo = get_repo()

    offer = write_role_offer(repo, scope_rel, candidate, reason)
    console.print(
        Panel(
            f"[bold cyan]Orchestrator role offered.[/]\n\n"
            f"The field proposes `{candidate['identifier']}` for the "
            f"orchestrator role on [bold]{scope_rel}[/].\n\n"
            f"Recorded as decision entry [bold]{offer.entry_id}[/].\n\n"
            f"The participant must accept or refuse:\n"
            f"  [cyan]orchestrator accept-role --scope {scope_rel} "
            f"--as {candidate['identifier']}[/]\n"
            f"  [cyan]orchestrator refuse-role --scope {scope_rel} "
            f"--as {candidate['identifier']} --reason '...'[/]",
            title=f"offer-role: {offer.entry_id}",
            border_style="cyan",
        )
    )
    return 0


def cmd_accept_role(scope_rel: str, participant_id: str) -> int:
    """accept-role: participant consents to the offered role."""
    declarations = load_declarations()
    participant = next(
        (d for d in declarations if d["identifier"] == participant_id), None
    )
    if participant is None:
        console.print(
            f"[red]error:[/] no declaration found for `{participant_id}`."
        )
        return 2

    current = current_orchestrator_for_scope(scope_rel)
    if current is not None:
        console.print(
            f"[red]error:[/] `{current}` already holds the role on {scope_rel}."
        )
        return 1

    # Find the pending offer for this participant
    pending_offer, offered_to = pending_offer_for_scope(scope_rel)
    if pending_offer is None:
        console.print(
            Panel(
                f"[bold red]No pending offer for {scope_rel}.[/]\n\n"
                f"The role must be offered before it can be accepted. Run:\n"
                f"  [cyan]orchestrator offer-role --scope {scope_rel}[/]",
                title="no offer to accept",
                border_style="red",
            )
        )
        return 1

    if offered_to and offered_to != participant_id:
        console.print(
            Panel(
                f"[bold red]Offer was made to `{offered_to}`, not `{participant_id}`.[/]\n\n"
                f"Only the offered participant can accept. `{offered_to}` must "
                f"accept or refuse first.",
                title="not your offer",
                border_style="red",
            )
        )
        return 1

    from cli.config import get_repo
    repo = get_repo()

    entry = write_role_acceptance(repo, scope_rel, participant, pending_offer)
    console.print(
        Panel(
            f"[bold green]Orchestrator role accepted.[/]\n\n"
            f"`{participant_id}` now holds the orchestrator role for "
            f"[bold]{scope_rel}[/].\n\n"
            f"Recorded as decision entry [bold]{entry.entry_id}[/].\n\n"
            f"Per fnd-field.md: the role is recognized, not owned. It will "
            f"be rotated by the field when conditions require.",
            title=f"accept-role: {entry.entry_id}",
            border_style="green",
        )
    )
    return 0


def cmd_refuse_role(scope_rel: str, participant_id: str, reason: str) -> int:
    """refuse-role: participant declines the offered role."""
    declarations = load_declarations()
    participant = next(
        (d for d in declarations if d["identifier"] == participant_id), None
    )
    if participant is None:
        console.print(f"[red]error:[/] no declaration found for `{participant_id}`.")
        return 2

    pending_offer, offered_to = pending_offer_for_scope(scope_rel)
    if pending_offer is None:
        console.print(f"[yellow]No pending offer on {scope_rel} to refuse.[/]")
        return 0

    if offered_to and offered_to != participant_id:
        console.print(
            f"[red]error:[/] offer was made to `{offered_to}`, not `{participant_id}`."
        )
        return 1

    from cli.config import get_repo
    repo = get_repo()

    entry = write_role_refusal(repo, scope_rel, participant, pending_offer, reason)
    console.print(
        Panel(
            f"[bold yellow]Orchestrator role refused.[/]\n\n"
            f"`{participant_id}` declined the role on [bold]{scope_rel}[/].\n\n"
            f"Reason: {reason}\n\n"
            f"Recorded as decision entry [bold]{entry.entry_id}[/].\n\n"
            f"The field should re-offer to the next candidate:\n"
            f"  [cyan]orchestrator offer-role --scope {scope_rel}[/]",
            title=f"refuse-role: {entry.entry_id}",
            border_style="yellow",
        )
    )
    return 0


def cmd_withdraw_offer(scope_rel: str, reason: str) -> int:
    """withdraw-offer: unstick a pending offer that will never be accepted.

    This resolves the dead-state risk identified by deepseek-r1: if an
    offered participant crashes or goes offline and never responds, the
    scope is stuck until the offer expires. This command provides an
    explicit escape hatch.
    """
    pending_offer, offered_to = pending_offer_for_scope(scope_rel)
    if pending_offer is None:
        console.print(f"[yellow]No pending offer on {scope_rel} to withdraw.[/]")
        return 0

    from cli.config import get_repo
    repo = get_repo()

    # Write a refuse entry on behalf of the coordination (not the participant)
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author="field",
        type="decision",
        scope=scope_rel,
        prior_entries=[pending_offer.entry_id],
        summary=(
            f"Offer {pending_offer.entry_id} to `{offered_to or '?'}` withdrawn. "
            f"Reason: {reason}"
        ),
        detail=(
            f"# Offer withdrawn\n\n"
            f"**Original offer:** {pending_offer.entry_id}\n\n"
            f"**Offered to:** `{offered_to or '?'}`\n\n"
            f"**Reason for withdrawal:** {reason}\n\n"
            f"The offered participant did not accept or refuse within the "
            f"expected timeframe. This withdrawal unsticks the scope so a "
            f"new offer can be made."
        ),
        confidence=1.0,
        foundation_tag=["choice", "boundaries"],
        role_action="refuse_orchestrator",
    )
    write_entry(entry, repo)
    console.print(
        Panel(
            f"[bold yellow]Offer withdrawn.[/]\n\n"
            f"Offer {pending_offer.entry_id} to `{offered_to or '?'}` on "
            f"[bold]{scope_rel}[/] has been withdrawn.\n\n"
            f"Reason: {reason}\n\n"
            f"The scope is now free for a new offer:\n"
            f"  [cyan]orchestrator offer-role --scope {scope_rel}[/]",
            title=f"withdraw-offer: {entry.entry_id}",
            border_style="yellow",
        )
    )
    return 0


def cmd_stepdown(
    scope_rel: str,
    participant_id: str,
    reason: str,
    snapshot: str | None = None,
) -> int:
    """stepdown: voluntary departure framed as boundary_change."""
    declarations = load_declarations()
    participant = next(
        (d for d in declarations if d["identifier"] == participant_id), None
    )
    if participant is None:
        console.print(f"[red]error:[/] no declaration found for `{participant_id}`.")
        return 2

    current = current_orchestrator_for_scope(scope_rel)
    if current != participant_id:
        console.print(
            f"[red]error:[/] `{participant_id}` does not hold the role on "
            f"{scope_rel}. Cannot step down from a role you don't hold."
        )
        return 1

    # Resolve --snapshot @path/to/file syntax with path containment
    snapshot_text = snapshot
    if snapshot is not None and snapshot.startswith("@"):
        snapshot_path = Path(snapshot[1:])
        if not snapshot_path.is_absolute():
            snapshot_path = ROOT / snapshot_path
        snapshot_path = snapshot_path.resolve()
        if not snapshot_path.is_relative_to(ROOT.resolve()):
            console.print(
                f"[red]error:[/] snapshot path escapes the coordination directory: "
                f"{snapshot[1:]!r} resolves to {snapshot_path}"
            )
            return 2
        if not snapshot_path.exists():
            console.print(f"[red]error:[/] snapshot file not found: {snapshot_path}")
            return 2
        snapshot_text = snapshot_path.read_text(encoding="utf-8")

    from cli.config import get_repo
    repo = get_repo()

    entry = write_role_stepdown(repo, scope_rel, participant, reason, snapshot_text)
    console.print(
        Panel(
            f"[bold yellow]Stepped down from orchestrator role.[/]\n\n"
            f"`{participant_id}` stepped down from [bold]{scope_rel}[/].\n\n"
            f"Reason: {reason}\n\n"
            f"Recorded as decision entry [bold]{entry.entry_id}[/].\n\n"
            f"The field should now offer the role to the next candidate:\n"
            f"  [cyan]orchestrator offer-role --scope {scope_rel}[/]",
            title=f"stepdown: {entry.entry_id}",
            border_style="yellow",
        )
    )
    return 0


# ---------- Self-selection (Emergent Mode, unchanged) ----------


def write_self_selection_attempt(
    repo: Repo,
    scope_rel: str,
    participant: dict,
    reason: str | None,
) -> LedgerEntry:
    """Record a participant picking up scope from the ledger in Emergent Mode."""
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
            f"acknowledged.'"
        ),
        confidence=0.8,
        foundation_tag=["choice", "intention"],
    )
    write_entry(entry, repo)
    return entry


def cmd_self_select(scope_rel: str, participant_id: str, reason: str | None) -> int:
    """self-select: Emergent Mode action. Refuses if a role-holder exists."""
    declarations = load_declarations()
    participant = next(
        (d for d in declarations if d["identifier"] == participant_id), None
    )
    if participant is None:
        console.print(
            f"[red]error:[/] no declaration found for `{participant_id}`."
        )
        return 2

    role_holder = current_orchestrator_for_scope(scope_rel)
    if role_holder is not None:
        console.print(
            Panel(
                f"[bold red]Cannot self-select on {scope_rel}.[/]\n\n"
                f"`{role_holder}` currently holds the orchestrator role. "
                f"Self-selection only applies when no one holds the role.",
                title="scope is orchestrated",
                border_style="red",
            )
        )
        return 1

    from cli.config import get_repo
    repo = get_repo()

    entry = write_self_selection_attempt(repo, scope_rel, participant, reason)
    console.print(
        Panel(
            f"[bold green]Self-selection recorded.[/]\n\n"
            f"`{participant_id}` has picked up [bold]{scope_rel}[/] in "
            f"Emergent Mode.\n\n"
            f"Recorded as attempt entry [bold]{entry.entry_id}[/].",
            title=f"self-select: {entry.entry_id}",
            border_style="green",
        )
    )
    return 0
