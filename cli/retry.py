"""LLM call retry logic, participant failure recording, and token usage tracking."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import litellm
from git import Repo
from pydantic import ValidationError

from cli.schema import LedgerEntry
from cli.config import console, load_config, resolve_provider_routing
from cli.ledger import next_entry_id, write_entry
from cli.parsing import extract_json, finalize_entry
from cli.signals import write_outgoing_handoff, process_signals_from_response
from cli.breakers import record_token_usage


def request_entry_with_retry(
    *,
    decl: dict,
    base_messages: list[dict],
    expected_types: tuple[str, ...],
    required_prior_entries: tuple[str, ...] = (),
    scope_path: str,
    max_retries: int = 2,
    repo: Repo | None = None,
    from_participant: str | None = None,
    handoff_task_type: str = "request_entry",
) -> tuple[LedgerEntry | None, str | None]:
    """Call a participant via LiteLLM, validate, retry-on-error.

    Returns (entry, None) on success, (None, last_error) on persistent
    failure. A response of type=failure is ALWAYS accepted as a refusal —
    the orchestrator does not coerce a refusal into the requested type.
    Author mismatch, type mismatch, missing required lineage, and schema
    violations all surface back to the agent as plain-language error
    signals so the agent can decide how to respond.
    """
    messages = list(base_messages)
    last_error: str | None = None
    max_tokens = decl.get("context_constraints", {}).get("token_budget_per_task", 8000)
    routing_kwargs = resolve_provider_routing(decl, load_config())

    # Write the orchestrator -> agent handoff envelope before the call.
    # This creates the trace; the agent's response will be processed below.
    if repo is not None and from_participant is not None:
        try:
            config = load_config()
            capture_prompt = config.get("capture_prompt_in_handoff", False)
            write_outgoing_handoff(
                repo=repo,
                from_participant=from_participant,
                to_agent=decl["identifier"],
                task_type=handoff_task_type,
                scope_path=scope_path,
                payload={
                    "expected_types": list(expected_types),
                    "required_prior_entries": list(required_prior_entries),
                },
                lineage=list(required_prior_entries),
                prompt_messages=base_messages if capture_prompt else None,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[yellow]warning: handoff envelope write failed: {e}[/]")

    for attempt in range(max_retries + 1):
        try:
            resp = litellm.completion(
                model=decl["litellm_model"],
                messages=messages,
                temperature=0.2,
                max_tokens=max_tokens,
                **routing_kwargs,
            )
            text = resp.choices[0].message.content or ""
        except Exception as e:  # noqa: BLE001
            return None, f"provider error: {e}"

        # Track token usage for the Resource circuit breaker
        usage = getattr(resp, "usage", None)
        if usage is not None:
            total_tokens = getattr(usage, "total_tokens", 0) or 0
            if total_tokens > 0:
                record_token_usage(decl["identifier"], total_tokens)

        # --- Process any out-of-band signals first ---
        # Signals are independent of entry validation: a valid signal is
        # valid even if the surrounding entry is broken. Processing happens
        # exactly once per response, on the first attempt only — retries
        # are about the entry, not about resending signals.
        if attempt == 0 and repo is not None:
            try:
                process_signals_from_response(text, decl, repo)
            except Exception as e:  # noqa: BLE001
                console.print(f"[yellow]warning: signal processing failed: {e}[/]")

        # --- Schema parse ---
        try:
            raw = extract_json(text)
            entry = finalize_entry(raw, decl["identifier"], scope_path)
        except (ValidationError, ValueError, json.JSONDecodeError) as e:
            last_error = f"parse/validation: {e}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your previous response could not be validated as a ledger entry:\n\n"
                    f"    {e}\n\n"
                    f"Please return one corrected JSON ledger entry, OR return a JSON "
                    f"entry with type=\"failure\" if you decline to retry, with `detail` "
                    f"explaining why. Refusal is signal — there is no penalty for refusing."
                )},
            ]
            continue

        # --- Refusal is always accepted ---
        if entry.type == "failure":
            return entry, None

        # --- Author check (write protocol: author matches signal origin) ---
        if entry.author != decl["identifier"]:
            last_error = f"author mismatch: expected {decl['identifier']}, got {entry.author}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your entry's author field is \"{entry.author}\" but you are "
                    f"\"{decl['identifier']}\" in this coordination. Per the ledger "
                    f"write protocol, author must match the signal origin. Please "
                    f"return a corrected entry with author=\"{decl['identifier']}\", "
                    f"or return a type=\"failure\" entry explaining the disagreement."
                )},
            ]
            continue

        # --- Type check ---
        if entry.type not in expected_types:
            last_error = f"type mismatch: expected one of {list(expected_types)}, got {entry.type}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"You returned an entry with type=\"{entry.type}\". This task "
                    f"expects one of: {list(expected_types)}.\n\n"
                    f"If your type=\"{entry.type}\" was a mistake, please return a "
                    f"corrected entry with one of the expected types. If it was "
                    f"intentional and you believe the task framing is wrong, return "
                    f"a type=\"failure\" entry with `detail` explaining the disagreement. "
                    f"Either response is signal."
                )},
            ]
            continue

        # --- Required prior_entries check ---
        missing_priors = [eid for eid in required_prior_entries if eid not in entry.prior_entries]
        if missing_priors:
            last_error = f"missing required prior_entries: {missing_priors}"
            if attempt == max_retries:
                return None, last_error
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your entry's prior_entries field is missing the following required "
                    f"lineage links: {missing_priors}\n\n"
                    f"These are the entries the orchestrator surfaced to you in the task "
                    f"framing. Future participants reading the ledger need this link to "
                    f"trace your work back to the convergence or question that prompted "
                    f"it (per fnd-ledger.md -> Recursion).\n\n"
                    f"Please return a corrected entry with these ids added to prior_entries, "
                    f"OR return a type=\"failure\" entry with `detail` explaining why you "
                    f"decline to link to them."
                )},
            ]
            continue

        # All checks passed.
        return entry, None

    return None, last_error


def write_participant_failure(
    repo: Repo,
    scope_path: str,
    decl: dict,
    error_text: str,
    triggering_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Record a participant's persistent inability to produce a valid entry.

    Per fnd-failure.md, this is a `failure` entry tagged with the foundations
    under strain. The participant retains all their participant rights — this
    entry is signal about the interaction, not a verdict on the participant.
    Authored by the role-holder who observed the failure while routing.
    """
    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="failure",
        scope=scope_path,
        prior_entries=[triggering_entry_id],
        summary=(
            f"Participant `{decl['identifier']}` could not produce a valid entry "
            f"under the current task framing after retries. Recorded as signal."
        ),
        detail=(
            f"# Participant could not converge on a valid entry\n\n"
            f"**Participant:** `{decl['identifier']}`\n\n"
            f"**Triggering entry:** {triggering_entry_id}\n\n"
            f"**Last validation error:**\n\n"
            f"    {error_text}\n\n"
            f"Per fnd-failure.md, this is recorded as signal, not as a verdict on the "
            f"participant. The orchestrator surfaced validation errors back to the "
            f"participant after each failed attempt; the participant did not amend "
            f"to a valid entry and did not return a `type=failure` refusal. The most "
            f"likely diagnoses are (a) task framing was unclear (Intention/Signal "
            f"concern), (b) the participant's context window was saturated, or "
            f"(c) the schema requirements were inconsistent with the participant's "
            f"capability envelope. The repair cycle should diagnose which."
        ),
        confidence=1.0,
        foundation_tag=["signal", "recursion"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry
