"""Orchestrator role management: take, release, transfer, self-select."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from git import Repo
from rich.panel import Panel

from cli.schema import LedgerEntry
from cli.config import ROOT, console, load_declarations
from cli.ledger import (
    next_entry_id,
    write_entry,
    entries_for_scope,
    load_entry,
)


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
    per fnd-participants.md -> Transfer: the new holder is acknowledging
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
            f"{acknowledging_release.entry_id}. Per fnd-participants.md -> "
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
    fnd-participants.md -> Transfer: 'The outgoing participant writes a
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
            f"Per fnd-participants.md -> Transfer, the outgoing participant "
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
    is the second step of a transfer per fnd-participants.md -> Transfer.
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

    from cli.config import get_repo

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


def cmd_release_role(
    scope_rel: str,
    participant_id: str,
    reason: str = "voluntary release",
    snapshot: str | None = None,
    transferring_to: str | None = None,
) -> int:
    """`release-role` subcommand. If snapshot is provided, this is the first
    step of a transfer per fnd-participants.md -> Transfer.
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

    from cli.config import get_repo

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


def write_self_selection_attempt(
    repo: Repo,
    scope_rel: str,
    participant: dict,
    reason: str | None,
) -> LedgerEntry:
    """Record a participant picking up scope from the ledger in Emergent Mode.

    Per fnd-participants.md -> Accept and fnd-field.md -> Emergent Mode:
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
            + f"Per fnd-field.md -> Emergent Mode: 'No one holds a special role. "
            f"Participants read the ledger, identify where they can contribute, "
            f"propose their involvement via signal, and begin work when "
            f"acknowledged.' This entry is `{participant['identifier']}`'s "
            f"declared involvement.\n\n"
            f"Per fnd-participants.md -> Accept: 'Acceptance is recorded as an "
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

    from cli.config import get_repo

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
