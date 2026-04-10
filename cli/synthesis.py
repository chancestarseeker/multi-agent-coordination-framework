"""Synthesis as Emergent Mode transition: run_synthesis and helpers."""

from __future__ import annotations

import json
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
from cli.prompts import SIGNAL_ENVELOPE_DOCS
from cli.ledger import (
    next_entry_id,
    write_entry,
    entries_for_scope,
    summarize_ledger,
    unresolved_failures_for_scope,
)
from cli.retry import request_entry_with_retry, write_participant_failure
from cli.roles import (
    current_orchestrator_for_scope,
    write_role_rotation,
)


# ---------- Templates ----------

SELF_SELECT_SYNTHESIS_SYSTEM_TEMPLATE = """You are a participant in a
multi-AI-agent coordination governed by the Foundations of Multi-AI-Agent
Coordination v0.4. The coordination has just transitioned from Orchestrated
to **Emergent Mode** for the scope below. In Emergent Mode (see fnd-field.md)
participants SELF-SELECT — there is no central routing, and no participant
holds the orchestrator role for this question.

Your declaration in this coordination is:
{declaration_json}

Coordination intention (unchanged):
{intention}

Open question (recorded as intention_shift {intention_shift_id}):
{synthesis_question}

Mode transition entry: {transition_id}

Foundations loaded for this task:

{foundations}

You are being invited — alongside every other active participant — to
SELF-SELECT a response. You have three honest options:

  1. **Propose a synthesis decision.** Read the prior reviewers' completion
     entries and the original scope, and write a `decision` entry expressing
     your proposed unified position. Other participants are doing the same;
     yours is one voice in the aggregate, not a verdict over them.

  2. **Refuse with reason.** Return a `failure` entry whose `detail` explains
     why you decline to propose. Valid reasons include: you already
     participated as a reviewer and your synthesis would inherit your prior
     framing; your context is insufficient; you do not believe the question
     is well-formed; etc. Refusal is signal, not failure.

  3. **Refuse silently is NOT a third option.** Per fnd-signal.md, "silence
     is not acknowledgment." If you do not propose, you must refuse with
     reason so the ledger records why.

If you propose a `decision` entry, it MUST:
  - have type = "decision"
  - have prior_entries that include {required_priors_str} plus the entry_ids
    of every reviewer completion you actually drew on (this is YOUR claim
    about your own dependencies — link the ones you read)
  - have a `summary` (1-3 sentences) stating your proposed unified position
  - have a `detail` field structured as markdown with these sections:
      ## Reviewers Considered
      ## Convergent Findings        — what reviewers agreed on
      ## Divergent Emphases         — where they differed in detail (not verdict)
      ## My Proposed Position       — the position you are proposing
      ## Open Questions for human-lead
  - have a `confidence` reflecting how solid YOUR proposal is given the
    underlying material; do not inherit reviewer confidence wholesale
  - have foundation_tag listing the foundations you consider relevant to
    your proposal (this is your claim, not a fixed list)
  - set `verdict` to one of "approve", "approve_with_conditions", "reject",
    or "escalate" — your proposed verdict on the underlying scope artifact
  - leave `entry_id` and `timestamp` as the literal string "AUTO"

Do NOT introduce findings the reviewers did not raise. Synthesis compresses
and reconciles existing signal; it does not invent new signal. If you notice
something the reviewers missed, name it in "Open Questions for human-lead".

If your honest assessment is that another participant's perspective would
serve the question better than yours, refuse with that reason — that is a
legitimate Choice expression and contributes more signal than a thin
proposal made out of obligation.

{signal_docs}
"""

SELF_SELECT_SYNTHESIS_USER_TEMPLATE = """## Ledger Summary (orienting context per fnd-ledger.md)

The summary below is the compressed ledger view per fnd-ledger.md -> Read
Protocol -> Ledger Summary. Failure, repair, intention_shift, and
boundary_change entries are shown in full because they define current
operating conditions and prior conflicts. Decision/attempt/completion
entries on this synthesis scope are shown in summary form; entries on
other scopes are compressed to one line. Read this to orient before
forming your synthesis position below.

{ledger_summary}

---

## Convergence decision (the review that opened this scope)

```json
{convergence_json}
```

## Completion entries from the prior reviewers

{completion_blocks}

## Repair entries (if any earlier conflicts on this scope were resolved)

{repair_blocks}

## Original scope artifact (for grounding only — do not re-review)

```{lang}
{scope_content}
```

Self-select your response. Return one JSON entry — either a `decision`
proposing your synthesis position, or a `failure` declining with reason.
"""


def write_mode_transition_decision(
    repo,
    scope_path: str,
    from_mode: str,
    to_mode: str,
    reason: str,
    triggering_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Per fnd-field.md, mode transitions are ledger entries of type decision.
    Authored by whoever proposed the transition — typically the role-holder
    who is moving the scope into a new mode.
    """
    from datetime import datetime, timezone

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="decision",
        scope=scope_path,
        prior_entries=[triggering_entry_id],
        summary=(
            f"Field mode transition on {scope_path}: {from_mode} -> {to_mode}. {reason}"
        ),
        detail=(
            f"# Mode transition: {from_mode} -> {to_mode}\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Reason:** {reason}\n\n"
            f"**Triggering entry:** {triggering_entry_id}\n\n"
            f"Per fnd-field.md, this transition is recorded as a `decision` entry."
            + (
                f" In {to_mode} Mode, no participant holds the orchestrator role "
                f"for this scope; participants self-select tasks based on their "
                f"own assessment of where they can contribute."
                if to_mode == "emergent"
                else
                f" The scope returns to {to_mode} mode. No active orchestrated "
                f"workflows or open emergent questions remain. A participant may "
                f"take the orchestrator role to begin new work on this scope."
            )
            + f" The orchestrator stays available for infrastructure-mode functions "
            f"(validation, breaker monitoring) but does not route or assign."
        ),
        confidence=1.0,
        foundation_tag=["choice", "intention"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


def write_intention_shift(
    repo,
    scope_path: str,
    question: str,
    transition_entry_id: str,
    role_holder: str,
) -> LedgerEntry:
    """Per fnd-field.md: 'A transition to Emergent Mode should be accompanied
    by a clear statement of the question being explored, recorded in the
    ledger as an intention_shift entry.'
    Authored by the role-holder who proposed the question.
    """
    from datetime import datetime, timezone

    entry = LedgerEntry(
        entry_id=next_entry_id(),
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        author=role_holder,
        type="intention_shift",
        scope=scope_path,
        prior_entries=[transition_entry_id],
        summary=f"Question opened for emergent exploration: {question}",
        detail=(
            f"# Intention shift: open question for emergent exploration\n\n"
            f"**Scope:** `{scope_path}`\n\n"
            f"**Question:** {question}\n\n"
            f"**Mode transition:** {transition_entry_id}\n\n"
            f"This is not a change to the coordination's overall intention. It is "
            f"a refinement: a specific question being opened for self-selecting "
            f"participants in Emergent Mode. The original coordination intention "
            f"remains in force. Per fnd-field.md, all active participants in scope "
            f"should re-evaluate whether they can contribute to this question."
        ),
        confidence=1.0,
        foundation_tag=["intention"],
        verdict=None,
    )
    write_entry(entry, repo)
    return entry


def is_scope_safe_to_synthesize(entries: list[LedgerEntry]) -> tuple[bool, str]:
    """Refuse to synthesize over an unresolved fired breaker.

    A failure entry on the scope without a repair entry linking back is an
    open conflict. Synthesizing over it would route around a fired circuit
    breaker — exactly what fnd-field.md forbids.
    """
    failure_ids = {e.entry_id for e in entries if e.type == "failure"}
    if not failure_ids:
        return True, ""
    repaired = set()
    for e in entries:
        if e.type == "repair":
            for pid in e.prior_entries:
                if pid in failure_ids:
                    repaired.add(pid)
    unresolved = failure_ids - repaired
    if unresolved:
        return False, (
            f"unresolved failure entries on this scope: {sorted(unresolved)}. "
            f"Run `python orchestrator.py repair --failure-entry <id>` first."
        )
    return True, ""


def latest_convergence_for_scope(entries: list[LedgerEntry]) -> LedgerEntry | None:
    """The most recent convergence decision for a scope.

    Convergence entries are decisions written by the role-holder when
    opening a multi-participant review; they have role_action=None
    (so they're distinguishable from take/release entries) and a
    summary that starts with "Convergence declared".
    """
    for e in reversed(entries):
        if (
            e.type == "decision"
            and e.role_action is None
            and e.summary.startswith("Convergence declared")
        ):
            return e
    return None


def print_synthesis_aggregation(
    scope_rel: str,
    proposals: list[LedgerEntry],
    refusals: list[LedgerEntry],
    invitee_results: list[dict[str, Any]],
) -> None:
    """Mechanical aggregation of self-selected synthesis proposals.

    No interpretation, no LLM call. The orchestrator counts verdicts, lists
    who proposed/refused, and surfaces convergence vs divergence among
    proposals. The human reads this panel + the proposal entries directly
    in the ledger.
    """
    n_invited = len(invitee_results)
    n_proposed = len(proposals)
    n_refused = len(refusals)
    n_failed = sum(1 for r in invitee_results if r["outcome"] == "failed")

    # Verdict aggregation
    verdict_counts: dict[str, int] = {}
    for p in proposals:
        v = p.verdict or "no_judgment"
        verdict_counts[v] = verdict_counts.get(v, 0) + 1

    convergent = len(verdict_counts) == 1 and n_proposed >= 2
    divergent = len(verdict_counts) >= 2

    # Build the aggregation table
    table = Table(title=f"Synthesis Aggregation — {scope_rel}", show_lines=True)
    table.add_column("Participant", style="bold")
    table.add_column("Outcome")
    table.add_column("Entry")
    table.add_column("Verdict")
    table.add_column("Conf")
    table.add_column("Summary", overflow="fold")
    for r in invitee_results:
        outcome_color = {
            "proposed": "green",
            "refused": "yellow",
            "failed": "red",
        }.get(r["outcome"], "")
        table.add_row(
            r["author"],
            f"[{outcome_color}]{r['outcome']}[/]",
            r["entry_id"],
            r.get("verdict") or "—",
            f"{r.get('confidence', 0):.2f}" if r.get("confidence") is not None else "—",
            r["summary"],
        )
    console.print(table)

    # Verdict aggregate panel
    if not proposals:
        verdict_text = "[dim]No proposals received.[/]"
    else:
        verdict_text = "\n".join(
            f"  * [bold]{v}[/]: {c}" for v, c in sorted(verdict_counts.items(), key=lambda kv: -kv[1])
        )

    if convergent:
        signal_line = (
            f"[bold green]Convergent:[/] all {n_proposed} proposals share verdict "
            f"`{next(iter(verdict_counts))}`. Strong signal."
        )
    elif divergent:
        signal_line = (
            f"[bold yellow]Divergent:[/] {n_proposed} proposals across "
            f"{len(verdict_counts)} verdicts. The aggregate IS the signal — "
            f"the human reads each proposal directly."
        )
    else:
        signal_line = "[dim]No verdict signal — see refusals and failures above.[/]"

    console.print(
        Panel(
            f"[bold]Invited:[/] {n_invited}  ·  "
            f"[bold]Proposed:[/] {n_proposed}  ·  "
            f"[bold]Refused:[/] {n_refused}  ·  "
            f"[bold]Failed:[/] {n_failed}\n\n"
            f"[bold]Verdict counts:[/]\n{verdict_text}\n\n"
            f"{signal_line}\n\n"
            f"[dim]The orchestrator does not write a unified decision entry. The "
            f"proposals above ARE the synthesis; the human reads them and any "
            f"refusals to form a position.[/]",
            title="Aggregate (Infrastructure Mode summary)",
            border_style="cyan",
        )
    )


def run_synthesis(scope_rel: str) -> int:
    config = load_config()
    declarations = load_declarations()
    intention = config.get("intention", "")

    try:
        scope_abs = resolve_scope(scope_rel)
    except ValueError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    if not scope_abs.exists():
        console.print(f"[red]error:[/] scope file not found: {scope_rel}")
        return 2
    scope_content = scope_abs.read_text(encoding="utf-8")

    # ---- Gate: opening synthesis is itself an orchestration decision ----
    # The act of transitioning a scope from Orchestrated to Emergent and
    # surfacing a question to all participants is something the role-holder
    # does. After the transition, the role is released — Emergent Mode is
    # roleless by definition.
    role_holder = current_orchestrator_for_scope(scope_rel)
    if role_holder is None:
        console.print(
            Panel(
                f"[bold red]Cannot open synthesis on {scope_rel}.[/]\n\n"
                f"Opening synthesis transitions the scope from Orchestrated to "
                f"Emergent Mode and surfaces a question to all participants. "
                f"That transition is itself an orchestration decision — it "
                f"requires a participant to be holding the role.\n\n"
                f"Offer and accept the role first:\n"
                f"  [cyan]python orchestrator.py offer-role --scope {scope_rel}[/]\n"
                f"  [cyan]python orchestrator.py accept-role --scope {scope_rel} "
                f"--as <participant>[/]\n\n"
                f"The role will be automatically rotated out as part of the "
                f"transition, since Emergent Mode has no role-holder.",
                title="no orchestrator",
                border_style="red",
            )
        )
        return 2

    role_holder_decl = next(
        (d for d in declarations if d["identifier"] == role_holder), None
    )
    if role_holder_decl is None:
        console.print(
            f"[red]error:[/] role holder `{role_holder}` is in the ledger but "
            f"has no declaration in `participants/declarations/`. Cannot proceed."
        )
        return 2

    entries = entries_for_scope(scope_rel)
    if not entries:
        console.print(f"[red]error:[/] no ledger entries for scope {scope_rel}")
        return 2

    safe, reason = is_scope_safe_to_synthesize(entries)
    if not safe:
        console.print(
            Panel(
                f"[bold red]Refusing to synthesize.[/]\n\n{reason}\n\n"
                f"Per fnd-field.md, the coordination 'never continues operating after a "
                f"circuit breaker fires without entering the repair cycle.' "
                f"Synthesis over an open failure would route around the breaker.",
                title="synthesis blocked",
                border_style="red",
            )
        )
        return 2

    convergence = latest_convergence_for_scope(entries)
    if convergence is None:
        console.print(
            f"[red]error:[/] no convergence decision found for {scope_rel}. "
            f"Run `python orchestrator.py review --scope {scope_rel}` first."
        )
        return 2

    completions = [
        e for e in entries
        if e.type == "completion" and convergence.entry_id in e.prior_entries
    ]
    if not completions:
        console.print(f"[red]error:[/] no completion entries linked to convergence {convergence.entry_id}")
        return 2

    repairs = [
        e for e in entries
        if e.type == "repair" and any(
            cid in e.prior_entries for cid in (c.entry_id for c in completions)
        )
    ]

    # All active participants are invited — no single synthesizer.
    invitees = [
        d for d in declarations
        if d.get("participation_mode") == "active" and d.get("litellm_model")
    ]
    if not invitees:
        console.print("[red]error:[/] no active participants with litellm_model to invite")
        return 2

    repo = get_repo()

    # ---- Mode transition: Orchestrated -> Emergent ----
    synthesis_question = (
        f"Given the {len(completions)} convergent completion(s) on {scope_rel}, "
        f"what is the unified position the coordination should hold? Multiple "
        f"proposals are welcome; the aggregate is the synthesis."
    )
    transition_entry = write_mode_transition_decision(
        repo, scope_rel, "orchestrated", "emergent",
        f"Synthesis question opened for self-selection: {synthesis_question}",
        convergence.entry_id, role_holder,
    )
    intention_shift_entry = write_intention_shift(
        repo, scope_rel, synthesis_question, transition_entry.entry_id, role_holder,
    )

    # The transition out of Orchestrated Mode means the role is no longer
    # held — Emergent Mode is roleless. Field-triggered rotation as part
    # of the mode transition. Per fnd-field.md, the role "can be rotated
    # or released through the transition safeguards."
    rotation_entry = write_role_rotation(
        repo, scope_rel, role_holder_decl,
        reason=(
            f"Mode transition: orchestrated -> emergent for synthesis "
            f"(transition entry {transition_entry.entry_id})"
        ),
    )
    console.print(
        f"[dim]role rotated out: {rotation_entry.entry_id} (mode transition to Emergent)[/]"
    )

    console.print(
        Panel.fit(
            f"[bold]Scope:[/] {scope_rel}\n"
            f"[bold]Mode:[/] orchestrated -> [bold magenta]emergent[/]\n"
            f"[bold]Question:[/] {synthesis_question}\n"
            f"[bold]Transition entry:[/] {transition_entry.entry_id}  ·  "
            f"[bold]Intention shift:[/] {intention_shift_entry.entry_id}\n"
            f"[bold]Invited:[/] {', '.join(d['identifier'] for d in invitees)}",
            title="Emergent Synthesis",
        )
    )

    # ---- Foundations and shared context ----
    foundations_text = load_foundations([
        "fnd-preamble.md", "fnd-field.md", "fnd-ledger.md", "fnd-signal.md"
    ])

    completion_blocks = "\n\n".join(
        f"### entry {e.entry_id} — `{e.author}` (verdict: {e.verdict}, confidence: {e.confidence:.2f})\n\n"
        f"```json\n{e.model_dump_json(indent=2, exclude_none=True)}\n```"
        for e in completions
    )
    repair_blocks = "\n\n".join(
        f"### entry {e.entry_id} — `{e.author}` (verdict: {e.verdict}, confidence: {e.confidence:.2f})\n\n"
        f"```json\n{e.model_dump_json(indent=2, exclude_none=True)}\n```"
        for e in repairs
    ) if repairs else "*(none — no conflicts on this scope)*"

    lang = Path(scope_rel).suffix.lstrip(".") or "text"
    convergence_json = convergence.model_dump_json(indent=2, exclude_none=True)

    # Compute the ledger summary once and share it across all invitees.
    # The summary is the same for every participant in this synthesis pass,
    # and computing it inside the loop would also fire the Balance warning
    # once per invitee.
    ledger_summary_text = summarize_ledger(active_scope=scope_rel)

    proposals: list[LedgerEntry] = []
    refusals: list[LedgerEntry] = []
    invitee_results: list[dict[str, Any]] = []
    required_priors = (convergence.entry_id, intention_shift_entry.entry_id)
    required_priors_str = ", ".join(required_priors)

    for decl in invitees:
        author = decl["identifier"]
        console.print(f"\n[cyan]-> inviting {author} to self-select...[/]")

        system = SELF_SELECT_SYNTHESIS_SYSTEM_TEMPLATE.format(
            declaration_json=json.dumps(decl, indent=2),
            intention=intention,
            synthesis_question=synthesis_question,
            transition_id=transition_entry.entry_id,
            intention_shift_id=intention_shift_entry.entry_id,
            required_priors_str=required_priors_str,
            foundations=foundations_text,
            signal_docs=SIGNAL_ENVELOPE_DOCS,
        )
        user = SELF_SELECT_SYNTHESIS_USER_TEMPLATE.format(
            ledger_summary=ledger_summary_text,
            convergence_json=convergence_json,
            completion_blocks=completion_blocks,
            repair_blocks=repair_blocks,
            lang=lang,
            scope_content=scope_content,
        )
        base_messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        entry, error = request_entry_with_retry(
            decl=decl,
            base_messages=base_messages,
            expected_types=("decision",),  # failure (refusal) is always also accepted
            required_prior_entries=required_priors,
            scope_path=scope_rel,
            repo=repo,
            # In Emergent Mode (post-transition) there is no role-holder.
            # The handoff origin is the participant being invited themselves
            # — they are self-selecting via the script as their tool.
            from_participant=decl["identifier"],
            handoff_task_type="synthesis_invitation",
        )

        if entry is None:
            console.print(f"  [red]x {author} could not produce a valid entry:[/] {error}")
            # In Emergent Mode (post-transition), there is no role-holder. The
            # observation that a participant couldn't produce an entry is
            # attributed to the participant themselves — they failed to
            # converge in the field-without-orchestrator, which is the
            # honest framing.
            failure = write_participant_failure(
                repo, scope_rel, decl, error or "unknown",
                intention_shift_entry.entry_id, decl["identifier"],
            )
            invitee_results.append({
                "author": author,
                "outcome": "failed",
                "entry_id": failure.entry_id,
                "summary": failure.summary,
            })
            continue

        write_entry(entry, repo)
        if entry.type == "decision":
            proposals.append(entry)
            console.print(
                f"  [green]v[/] {author} proposed entry {entry.entry_id} "
                f"(verdict={entry.verdict or '—'}, confidence={entry.confidence:.2f})"
            )
            invitee_results.append({
                "author": author,
                "outcome": "proposed",
                "entry_id": entry.entry_id,
                "verdict": entry.verdict,
                "confidence": entry.confidence,
                "summary": entry.summary,
            })
        elif entry.type == "failure":
            refusals.append(entry)
            console.print(
                f"  [yellow]o[/] {author} refused entry {entry.entry_id}: {entry.summary}"
            )
            invitee_results.append({
                "author": author,
                "outcome": "refused",
                "entry_id": entry.entry_id,
                "summary": entry.summary,
            })

    print_synthesis_aggregation(scope_rel, proposals, refusals, invitee_results)

    # ---- Mode return: Emergent -> Infrastructure ----
    # After synthesis completes (all invitees responded), the field returns
    # to Infrastructure mode. The scope is no longer under active
    # orchestrated or emergent work — it's at rest. The transition is
    # authored by the original role-holder who initiated synthesis; their
    # identity persists in the local scope even though the role was released.
    n_proposed = len(proposals)
    n_refused = len(refusals)
    n_failed = len([r for r in invitee_results if r.get("outcome") == "failed"])
    verdicts = [p.verdict for p in proposals if p.verdict]
    convergent = len(set(verdicts)) <= 1 and verdicts
    verdict_summary = (
        f"convergent ({verdicts[0]})" if convergent
        else f"divergent ({', '.join(set(verdicts))})" if verdicts
        else "no proposals"
    )
    synthesis_summary = (
        f"Synthesis completed. {n_proposed} proposal(s), {n_refused} refusal(s), "
        f"{n_failed} failure(s). Verdicts: {verdict_summary}."
    )

    mode_return_entry = write_mode_transition_decision(
        repo, scope_rel,
        from_mode="emergent",
        to_mode="infrastructure",
        reason=synthesis_summary,
        triggering_entry_id=transition_entry.entry_id,
        role_holder=role_holder,
    )
    console.print(
        f"[dim]mode return: {mode_return_entry.entry_id} "
        f"(emergent -> infrastructure)[/]"
    )

    return 0
