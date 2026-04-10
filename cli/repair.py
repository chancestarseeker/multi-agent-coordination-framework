"""Repair cycle: run_repair, run_verification_rerun, and templates."""

from __future__ import annotations

import json
from pathlib import Path

from rich.panel import Panel

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
    write_entry,
    load_entry,
    summarize_ledger,
)
from cli.retry import request_entry_with_retry, write_participant_failure
from cli.roles import current_orchestrator_for_scope


REPAIR_SYSTEM_TEMPLATE = """You are acting as the ARBITER in a repair cycle
for a multi-AI-agent coordination governed by the Foundations of Multi-AI-Agent
Coordination v0.4. A circuit breaker has fired and the coordination is paused.

Your declaration in this coordination is:
{declaration_json}

Coordination intention:
{intention}

The repair cycle is defined in the foundations loaded below. Read fnd-failure.md
and fnd-repair.md as your operating contract for this task.

{foundations}

You will be given:
  1. A `failure` ledger entry recording which breaker fired and why
  2. The completion entries the convergent reviewers wrote, which contain
     their incompatible verdicts and full reasoning

Your job is to perform the repair cycle (Pause -> Diagnose -> Surface -> Resolve
-> Verify -> Record), then return ONE JSON ledger entry of type `repair`. No
prose before or after the JSON.

The `repair` entry MUST:
  - have type = "repair"
  - have prior_entries that include the failure entry's id and the entry_ids
    of every completion entry you considered
  - have a `summary` that names the diagnosed root cause in 1-3 sentences
  - have a `detail` field structured with: ## Diagnosis, ## Resolution,
    ## Verification (or why rerun is unsafe), ## Lessons. Markdown is fine.
  - have a `confidence` reflecting how sure you are the resolution will hold
  - have foundation_tag listing which foundations are relevant (truth,
    boundaries, balance, etc.)
  - leave `entry_id` and `timestamp` as the literal string "AUTO"
  - set `verdict` to one of: "approve", "approve_with_conditions", "reject",
    "escalate" — your judgment of the underlying scope artifact AFTER the
    repair, since the repair must yield a coherent decision the coordination
    can act on. Use "escalate" if even with diagnosis you cannot pick.

Per fnd-repair.md: good faith first, full signal, accountability without
annihilation, demonstrated change, restoration. The repair is not a verdict
on the reviewers — it is a re-establishment of coherent shared truth.

{signal_docs}
"""

REPAIR_USER_TEMPLATE = """## Ledger Summary (orienting context per fnd-ledger.md)

The summary below is the compressed ledger view per fnd-ledger.md -> Read
Protocol -> Ledger Summary. Failure, repair, intention_shift, and
boundary_change entries are shown in full because they carry the highest
signal density for diagnosing what went wrong. Other entries are compressed.
Read this to orient before diagnosing the specific failure below.

{ledger_summary}

---

## Failure entry (the breaker that fired)

```json
{failure_json}
```

## Completion entries from the convergent reviewers

{completion_blocks}

Diagnose, resolve, and return one `repair` JSON entry.
"""


VERIFICATION_SYSTEM_TEMPLATE = """You are a participant in a multi-AI-agent coordination.
You are performing a VERIFICATION RERUN — a limited re-review of a scope
artifact under conditions that have been modified by a repair entry.

Your declaration:
{declaration_json}

Coordination intention:
{intention}

Foundations:
{foundations}

The repair entry below resolved a conflict or failure on this scope.
Your task is to re-review the scope artifact under the resolved conditions
and confirm whether the repair holds. Produce a `completion` entry with:
  - verdict: one of approve, approve_with_conditions, reject, escalate
  - prior_entries MUST include "{repair_entry_id}"
  - summary: whether the scope is now fit under the repaired conditions
  - detail: your full reasoning

If the same issue that triggered the original failure recurs, that is
strong signal that the repair did not hold. Return your honest assessment.

{signal_docs}
"""

VERIFICATION_USER_TEMPLATE = """## Repair entry (the resolution being verified)

```json
{repair_json}
```

## Original failure entry

```json
{failure_json}
```

## Scope artifact

```{lang}
{scope_content}
```

Re-review the scope under the repaired conditions. Return one JSON ledger entry.
"""


def run_verification_rerun(
    repair: LedgerEntry,
    failure: LedgerEntry,
    original_completions: list[LedgerEntry],
    repo,
    config: dict,
    role_holder: str,
) -> int:
    """Per fnd-repair.md: 'Verification must either include a limited rerun
    of the failed work under the resolved conditions, or explicitly record
    why rerun is impossible or unsafe.'

    This runs the original failing participants (extracted from the
    completions linked to the failure) through a verification review,
    with the repair entry as additional context.
    """
    declarations = load_declarations()
    intention = config.get("intention", "")
    foundations_text = load_foundations([
        "fnd-preamble.md", "fnd-failure.md", "fnd-repair.md", "fnd-ledger.md"
    ])

    # Determine who to re-verify with: the original participants who produced
    # the conflicting completions
    original_authors = {e.author for e in original_completions}
    verify_agents = [
        d for d in declarations
        if d["identifier"] in original_authors and d.get("litellm_model")
    ]

    if not verify_agents:
        console.print(
            "[yellow]No original reviewers available for verification rerun "
            "(they may lack litellm_model). Recording verification as skipped.[/]"
        )
        return 0

    try:
        scope_abs = resolve_scope(failure.scope)
    except ValueError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    if not scope_abs.exists():
        console.print(f"[red]error:[/] scope file not found for verification: {failure.scope}")
        return 2
    scope_content = scope_abs.read_text(encoding="utf-8")
    lang = Path(failure.scope).suffix.lstrip(".") or "text"

    any_failed = False
    for decl in verify_agents:
        author = decl["identifier"]
        console.print(f"\n[cyan]-> verification rerun for {author}...[/]")

        system = VERIFICATION_SYSTEM_TEMPLATE.format(
            declaration_json=json.dumps(decl, indent=2),
            intention=intention,
            foundations=foundations_text,
            repair_entry_id=repair.entry_id,
            signal_docs=SIGNAL_ENVELOPE_DOCS,
        )
        user = VERIFICATION_USER_TEMPLATE.format(
            repair_json=repair.model_dump_json(indent=2, exclude_none=True),
            failure_json=failure.model_dump_json(indent=2, exclude_none=True),
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
            expected_types=("completion",),
            required_prior_entries=(repair.entry_id,),
            scope_path=failure.scope,
            repo=repo,
            from_participant=role_holder,
            handoff_task_type="verification_rerun",
        )

        if entry is None:
            console.print(f"  [red]x {author} could not produce a verification entry:[/] {error}")
            any_failed = True
            continue

        write_entry(entry, repo)
        console.print(
            f"  [green]v[/] {author} verification: verdict={entry.verdict or '—'}, "
            f"confidence={entry.confidence:.2f}"
        )

        if entry.verdict in ("reject", "escalate"):
            console.print(
                f"  [yellow]! {author}'s verification suggests the repair may not hold.[/]"
            )
            any_failed = True

    return 1 if any_failed else 0


def run_repair(failure_entry_id: str, arbiter_id: str | None, verify: bool = False) -> int:
    config = load_config()
    declarations = load_declarations()
    intention = config.get("intention", "")

    # Load failure entry and the linked completions.
    try:
        failure = load_entry(failure_entry_id)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/] {e}")
        return 2
    if failure.type != "failure":
        console.print(f"[red]error:[/] entry {failure_entry_id} is type={failure.type}, not failure")
        return 2

    # ---- Gate: routing the repair to an arbiter requires the orchestrator role ----
    role_holder = current_orchestrator_for_scope(failure.scope)
    if role_holder is None:
        console.print(
            Panel(
                f"[bold red]Cannot route repair on {failure.scope}.[/]\n\n"
                f"No participant currently holds the orchestrator role for this "
                f"scope. Choosing an arbiter and routing the repair task to them "
                f"is an orchestration decision — it cannot be done without a "
                f"role-holder.\n\n"
                f"Offer and accept the role first:\n"
                f"  [cyan]python orchestrator.py offer-role --scope {failure.scope}[/]\n"
                f"  [cyan]python orchestrator.py accept-role --scope {failure.scope} "
                f"--as <participant>[/]",
                title="no orchestrator",
                border_style="red",
            )
        )
        return 2

    completion_ids = [
        eid for eid in failure.prior_entries
        if not eid.startswith(failure.entry_id)
    ]
    completions: list[LedgerEntry] = []
    for eid in completion_ids:
        try:
            e = load_entry(eid)
            if e.type == "completion":
                completions.append(e)
        except FileNotFoundError:
            console.print(f"[yellow]warning:[/] linked entry {eid} not found, skipping")

    if not completions:
        console.print("[red]error:[/] no completion entries linked from failure; nothing to arbitrate")
        return 2

    # Pick the arbiter.
    arbiter_id = arbiter_id or config.get("convergence", {}).get("arbiter") or "human-lead"
    arbiter = next((d for d in declarations if d["identifier"] == arbiter_id), None)
    if arbiter is None:
        console.print(f"[red]error:[/] no declaration found for arbiter '{arbiter_id}'")
        return 2
    if not arbiter.get("litellm_model"):
        console.print(
            Panel(
                f"[bold]Arbiter[/] [italic]{arbiter_id}[/] has no litellm_model — cannot run automated repair.\n\n"
                f"This is the expected case for [bold]human-lead[/]. The repair cycle is now [bold]your[/] turn:\n"
                f"  1. Read fnd-repair.md\n"
                f"  2. Read failure entry [bold]{failure.entry_id}[/] and its linked completions\n"
                f"  3. Hand-write a `repair` ledger entry that links back via prior_entries\n\n"
                f"Or rerun with [cyan]--arbiter <agent>[/] naming an agent that has a litellm_model.",
                title="repair: human turn",
                border_style="yellow",
            )
        )
        return 0

    # Load the broader foundations needed for repair.
    repair_foundations = load_foundations(
        ["fnd-preamble.md", "fnd-failure.md", "fnd-repair.md", "fnd-ledger.md"]
    )

    completion_blocks = "\n\n".join(
        f"### entry {e.entry_id} — `{e.author}` (verdict: {e.verdict}, confidence: {e.confidence:.2f})\n\n"
        f"```json\n{e.model_dump_json(indent=2, exclude_none=True)}\n```"
        for e in completions
    )

    system = REPAIR_SYSTEM_TEMPLATE.format(
        declaration_json=json.dumps(arbiter, indent=2),
        intention=intention,
        foundations=repair_foundations,
        signal_docs=SIGNAL_ENVELOPE_DOCS,
    )
    user = REPAIR_USER_TEMPLATE.format(
        ledger_summary=summarize_ledger(active_scope=failure.scope),
        failure_json=failure.model_dump_json(indent=2, exclude_none=True),
        completion_blocks=completion_blocks,
    )

    console.print(
        Panel.fit(
            f"[bold]Arbiter:[/] {arbiter_id} ({arbiter['litellm_model']})\n"
            f"[bold]Failure:[/] {failure.entry_id} on {failure.scope}\n"
            f"[bold]Considering:[/] {', '.join(e.entry_id for e in completions)}",
            title="Repair Cycle",
        )
    )

    repo = get_repo()
    base_messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    required_priors = (failure.entry_id,) + tuple(e.entry_id for e in completions)
    repair, error = request_entry_with_retry(
        decl=arbiter,
        base_messages=base_messages,
        expected_types=("repair",),
        required_prior_entries=required_priors,
        scope_path=failure.scope,
        repo=repo,
        from_participant=role_holder,
        handoff_task_type="repair",
    )
    if repair is None:
        console.print(f"[red]repair failed:[/] {error}")
        write_participant_failure(
            repo, failure.scope, arbiter, error or "unknown",
            failure.entry_id, role_holder,
        )
        return 1
    if repair.type == "failure":
        console.print(
            Panel(
                f"[yellow]Arbiter refused to produce a repair entry.[/]\n\n"
                f"{repair.summary}\n\n"
                f"Per fnd-repair.md, the arbiter's refusal is itself a repair-cycle "
                f"signal. The cycle is not closed — try a different arbiter, or "
                f"escalate to human-lead.",
                title=f"refusal {repair.entry_id}",
                border_style="yellow",
            )
        )
        write_entry(repair, repo)
        return 1
    path = write_entry(repair, repo)
    console.print(
        Panel(
            f"[bold]Repair entry written:[/] {path.name}\n"
            f"[bold]Verdict:[/] {repair.verdict or '—'}  ·  [bold]Confidence:[/] {repair.confidence:.2f}\n\n"
            f"{repair.summary}",
            title=f"repair {repair.entry_id}",
            border_style="green",
        )
    )

    # ---- Verification rerun (opt-in via --verify) ----
    if verify:
        console.print("\n[bold cyan]Running verification rerun...[/]")
        verify_result = run_verification_rerun(
            repair, failure, completions, repo, config, role_holder,
        )
        if verify_result != 0:
            console.print(
                "[yellow]Verification rerun did not fully pass. "
                "Check the new ledger entries for details.[/]"
            )
            return verify_result
        console.print("[green]Verification rerun completed successfully.[/]")

    return 0
