"""Circuit breakers: confidence, conflict, repetition, resource, and timeout."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from git import Repo

from cli.schema import LedgerEntry, SignalEnvelope
from cli.config import SIGNAL_ARCHIVE, console, load_declarations
from cli.ledger import next_entry_id, write_entry, entries_for_scope, unresolved_failures_for_scope
from cli.signals import ensure_signal_dirs


def write_confidence_failure(
    repo: Repo | None,
    scope_path: str,
    entry: LedgerEntry,
    confidence_floor: float,
    role_holder: str,
) -> LedgerEntry:
    """The Confidence circuit breaker.

    Per fnd-failure.md: fires when a participant reports confidence below
    the floor on a task that has no fallback routing. Writes a failure
    entry and signals the coordination to enter the repair cycle.
    """
    failure = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[entry.entry_id],
        summary=(
            f"Confidence circuit breaker fired: `{entry.author}` reported "
            f"confidence {entry.confidence:.2f} on {scope_path} "
            f"(floor: {confidence_floor})."
        ),
        detail=(
            f"# Confidence circuit breaker fired\n\n"
            f"**Participant:** `{entry.author}`\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Reported confidence:** {entry.confidence:.2f}\n\n"
            f"**Floor threshold:** {confidence_floor}\n\n"
            f"**Entry:** {entry.entry_id}\n\n"
            f"Per fnd-failure.md, the Confidence breaker fires when a "
            f"participant reports confidence below the floor. This indicates "
            f"the participant is uncertain about the quality of their work — "
            f"that uncertainty is signal, not failure. The repair cycle should "
            f"diagnose whether the task framing, the scope, or the participant "
            f"match is the issue."
        ),
        confidence=1.0,
        foundation_tag=["truth", "signal"],
    )
    write_entry(failure, repo)
    return failure


def _normalize_verdict(verdict: str) -> str:
    """Normalize verdicts into families for conflict detection.

    Per deepseek-r1 review: `approve` and `approve_with_conditions` are
    compatible positions (both approve the artifact). Treating them as
    incompatible forces unnecessary repair cycles. We normalize them into
    families: approve-family vs non-approve.
    """
    if verdict in ("approve", "approve_with_conditions"):
        return "approve_family"
    return verdict


def detect_verdict_conflict(entries: list[LedgerEntry]) -> bool:
    """The Conflict breaker.

    Two or more completion entries on the same scope with verdicts in
    different families are treated as incompatible state proposals.
    `approve` and `approve_with_conditions` are in the same family
    (both approve). `no_judgment` and `None` are abstentions.
    """
    verdicts = {
        _normalize_verdict(e.verdict)
        for e in entries
        if e.verdict and e.verdict != "no_judgment"
    }
    return len(verdicts) >= 2


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
                f"     [cyan]orchestrator repair --failure-entry {e.entry_id}[/]"
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
    exceeds N x the per-participant average (N = config circuit_breakers.resource_multiplier,
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
            f"tokens ({offender_tokens / average:.1f}x the {average:,.0f}-token average). "
            f"Threshold: {multiplier}x."
        ),
        detail=(
            f"# Resource circuit breaker fired\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Offender:** `{offender_id}` — {offender_tokens:,} tokens\n\n"
            f"**Average across {n_participants} participants:** {average:,.0f} tokens\n\n"
            f"**Multiplier threshold:** {multiplier}x = {threshold:,.0f} tokens\n\n"
            f"**Session usage by participant:**\n\n"
            + "\n".join(f"- `{pid}`: {t:,} tokens" for pid, t in _session_token_usage.items())
            + "\n\n"
            f"Per fnd-failure.md, the Resource circuit breaker fires when one "
            f"participant's consumption exceeds {multiplier}x the per-participant "
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
    ensure_signal_dirs()
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
            f"[red]Timeout breaker fired:[/] signal {sig_id} -> "
            f"`{destination}` ({elapsed:.0f}s > {tolerance}s)"
        )

    return failures
