"""Signal I/O, handlers, and processing for out-of-band signal envelopes."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock
from git import Repo
from pydantic import ValidationError
from rich.panel import Panel

from cli.schema import SignalEnvelope, LedgerEntry, VALID_SIGNAL_TYPES
from cli.config import SIGNAL_INBOX, SIGNAL_ARCHIVE, ROOT, console
from cli.ledger import next_entry_id, write_entry
from cli.parsing import extract_all_json, classify_json_object, finalize_entry


def ensure_signal_dirs() -> None:
    SIGNAL_INBOX.mkdir(parents=True, exist_ok=True)
    SIGNAL_ARCHIVE.mkdir(parents=True, exist_ok=True)


_signal_lock = None


def _get_signal_lock() -> FileLock:
    global _signal_lock
    if _signal_lock is None:
        ensure_signal_dirs()
        _signal_lock = FileLock(str(SIGNAL_ARCHIVE / ".lock"))
    return _signal_lock


def _next_signal_id() -> str:
    """Monotonic signal id, scoped to inbox + archive, with cross-platform lock."""
    ensure_signal_dirs()
    with _get_signal_lock():
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


def write_signal_to_inbox(envelope: SignalEnvelope) -> Path:
    """Persist a signal envelope as pending in signal/inbox/."""
    ensure_signal_dirs()
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
    """Write a `handoff` signal envelope for an orchestrator -> agent call.

    Per fnd-preamble.md, every message between participants carries a signal
    envelope. The orchestrator -> agent direction is currently implicit in the
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
    ensure_signal_dirs()
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
            f"{from_participant} -> {to_agent}: {task_type} task on {scope_path}"
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
                f"signal: handoff {envelope.signal_id} {from_participant} -> {to_agent}"
            )
        except Exception:  # noqa: BLE001
            pass
    return envelope


def archive_signal(envelope: SignalEnvelope, repo: Repo | None) -> Path:
    """Move a processed signal from inbox to archive (and git-track the archive copy)."""
    ensure_signal_dirs()
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

    Per fnd-participants.md -> Discovery, recommendations are recorded in
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
                f"Per fnd-participants.md -> Discovery, recommendations are recorded "
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
                f"coordination should consider entering the repair cycle."
            ),
            foundation_tag=list(cited_foundations),
            scope=payload.get("scope", "coordination"),
        )
        write_entry(entry, repo)
        # Update the detail with the actual entry_id now that it's assigned
        # (avoids the stale next_entry_id() hint bug)
        entry = entry.model_copy(update={
            "detail": entry.detail + (
                f"\n\nRun: `orchestrator repair "
                f"--failure-entry {entry.entry_id}` to begin the repair cycle."
            )
        })
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
                f"[bold]State update -> ledger entry[/]\n"
                f"[bold]From:[/] `{envelope.origin}` · [bold]Entry:[/] {entry.entry_id}\n"
                f"[bold]Type:[/] {entry.type} · [bold]Scope:[/] `{entry.scope}`\n\n"
                f"{entry.summary}",
                title=f"signal: state_update -> {path.name}",
                border_style="green",
            )
        )
        return entry

    # Case 2: scope + state fields -> synthesize an attempt entry
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
                f"[bold]State update -> attempt entry[/]\n"
                f"[bold]From:[/] `{envelope.origin}` · [bold]Scope:[/] `{scope}`\n\n"
                f"{state}",
                title=f"signal: state_update -> {entry.entry_id}",
                border_style="green",
            )
        )
        return entry

    # Case 3: unstructured — surface to console only
    console.print(
        Panel(
            f"[bold]State update (unstructured)[/]\n"
            f"[bold]From:[/] `{envelope.origin}` -> `{envelope.destination}`\n\n"
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

    - accept / accept-with-conditions -> write an `attempt` entry
      (per fnd-participants.md, acceptance is recorded as an attempt entry).
    - refuse-with-reason -> write a `decision` entry recording the refusal
      so other participants can see the scope is available.
    - no response field -> surface to console only (backward compat).
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
                title=f"signal: acknowledgment -> attempt {entry.entry_id}",
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
                f"Per fnd-participants.md -> Refuse, the scope remains available "
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
                title=f"signal: acknowledgment -> decision {entry.entry_id}",
                border_style="yellow",
            )
        )
        return entry

    # Unstructured acknowledgment — surface only
    console.print(
        Panel(
            f"[bold]Acknowledgment received[/]\n"
            f"[bold]From:[/] `{envelope.origin}` -> `{envelope.destination}`\n"
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
            f"[bold]From:[/] `{envelope.origin}` -> [bold]To:[/] `{envelope.destination}`\n"
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
    ensure_signal_dirs()
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
        # Author check: enforce that signal origin matches the authenticated
        # source. Per fnd-field.md: "Write to the ledger on behalf of a
        # participant without that participant's signal" is forbidden. A
        # mismatched origin would produce ledger entries attributed to the
        # wrong participant via signal handlers.
        if envelope.origin != source_decl["identifier"]:
            console.print(
                f"[yellow]warning: signal {envelope.signal_id} claims origin "
                f"`{envelope.origin}` but came from `{source_decl['identifier']}`. "
                f"Overwriting origin to match authenticated source.[/]"
            )
            envelope = envelope.model_copy(update={"origin": source_decl["identifier"]})
        signals.append(envelope)
        process_signal(envelope, repo)
    return signals
