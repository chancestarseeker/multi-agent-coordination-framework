"""
Minimal orchestrator for the Multi-Agent Coordination Framework.

Implements §7 of coordination-tech-stack.md as a small, honest first loop:
load declarations, load foundations, hand a single scope artifact to each
active agent, parse the proposed ledger entry from each response, validate
it, append it to the ledger, and git-commit.

What this orchestrator does NOT yet do (deliberately deferred):
  - signal envelope inbox/archive plumbing
  - circuit breaker enforcement (only confidence is checked, as a warning)
  - routing decisions (every active agent reviews the same scope)

Run:
    python orchestrator.py review --scope scope/code/example_auth.py
"""

from __future__ import annotations

import argparse
import json
import sys

from pydantic import ValidationError

from cli.schema import SignalEnvelope, LedgerEntry  # noqa: F401 — re-exported for compat
from cli.config import (
    console,
    SIGNAL_INBOX,
    SIGNAL_ARCHIVE,
    get_repo,
)
from cli.signals import (
    _ensure_signal_dirs,
    SIGNAL_HANDLERS,
    handle_default,
    archive_signal,
    process_signal,
)
from cli.breakers import check_timeout_breaker
from cli.ledger import print_ledger, print_ledger_summary
from cli.review import run_review
from cli.repair import run_repair
from cli.synthesis import run_synthesis
from cli.roles import cmd_take_role, cmd_release_role, cmd_self_select


# ---------- Inbox subcommands ----------

def inbox_list() -> int:
    _ensure_signal_dirs()
    pending = sorted(SIGNAL_INBOX.glob("*.json"))
    archived = sorted(SIGNAL_ARCHIVE.glob("*.json"))

    if not pending and not archived:
        console.print("[dim]signal inbox and archive are empty[/]")
        return 0

    if pending:
        console.print(f"\n[bold yellow]Pending in signal/inbox/[/] ({len(pending)}):")
        for p in pending:
            try:
                env = SignalEnvelope(**json.loads(p.read_text(encoding="utf-8")))
                console.print(
                    f"  * [bold]{env.signal_id}[/]  type={env.type}  "
                    f"from={env.origin} -> {env.destination}  conf={env.confidence:.2f}"
                )
                console.print(f"    [dim]{env.context_summary}[/]")
            except (ValidationError, json.JSONDecodeError) as e:
                console.print(f"  * [red]{p.name}[/] (invalid: {e})")

    if archived:
        console.print(f"\n[bold cyan]Archived in signal/archive/[/] ({len(archived)}):")
        for p in archived:
            try:
                env = SignalEnvelope(**json.loads(p.read_text(encoding="utf-8")))
                console.print(
                    f"  * [bold]{env.signal_id}[/]  type={env.type}  "
                    f"from={env.origin}  conf={env.confidence:.2f}"
                )
                console.print(f"    [dim]{env.context_summary}[/]")
            except (ValidationError, json.JSONDecodeError) as e:
                console.print(f"  * [red]{p.name}[/] (invalid: {e})")

    return 0


def inbox_process() -> int:
    """Process every signal currently in signal/inbox/.

    This is the offline test entry point: drop a hand-written JSON envelope
    into signal/inbox/, run this command, and watch it dispatch through the
    handlers without any API calls.
    """
    _ensure_signal_dirs()
    pending = sorted(SIGNAL_INBOX.glob("*.json"))
    if not pending:
        console.print("[dim]signal/inbox/ is empty — nothing to process[/]")
        return 0

    repo = get_repo()
    n_processed = 0
    n_failed = 0

    console.print(f"[bold]Processing {len(pending)} signal(s) from inbox...[/]\n")

    for p in pending:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            envelope = SignalEnvelope(**data)
        except (ValidationError, json.JSONDecodeError) as e:
            console.print(f"[red]x {p.name} is invalid:[/] {e}")
            n_failed += 1
            continue

        try:
            handler = SIGNAL_HANDLERS.get(envelope.type, handle_default)
            handler(envelope, repo)
            archive_signal(envelope, repo)
            n_processed += 1
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]x handler error on {envelope.signal_id}:[/] {e}")
            n_failed += 1

    console.print(
        f"\n[bold]Done.[/] Processed: {n_processed}  ·  Failed: {n_failed}"
    )

    # Run the Timeout circuit breaker after processing all pending signals
    repo = get_repo()
    timeout_failures = check_timeout_breaker(repo)
    if timeout_failures:
        console.print(
            f"\n[bold red]Timeout breaker fired for {len(timeout_failures)} "
            f"signal(s).[/] Run `python orchestrator.py ledger` to see failure entries."
        )

    return 0 if n_failed == 0 else 1


# ---------- CLI ----------

def main() -> int:
    parser = argparse.ArgumentParser(prog="orchestrator", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_review = sub.add_parser(
        "review",
        help="Send a scope artifact to agents (capability-routed if --task-type or inferred)",
    )
    p_review.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the artifact to review",
    )
    p_review.add_argument(
        "--task-type",
        default=None,
        help="Task type for capability-based routing (e.g., code_review, writing_review). "
             "If omitted, inferred from the scope file extension. Agents whose "
             "preferred_tasks include this type are preferred; if none match, all "
             "active agents review the scope (backward compatible broadcast).",
    )

    p_repair = sub.add_parser(
        "repair", help="Run the repair cycle for a fired failure entry"
    )
    p_repair.add_argument(
        "--failure-entry",
        required=True,
        help="entry_id of the failure that fired the breaker (e.g. '004')",
    )
    p_repair.add_argument(
        "--arbiter",
        default=None,
        help="Identifier of the participant to act as arbiter (defaults to config.convergence.arbiter)",
    )
    p_repair.add_argument(
        "--verify",
        action="store_true",
        help="After repair, automatically re-run the original failing reviewers "
             "under the resolved conditions. Per fnd-repair.md: verification "
             "must include a limited rerun or explicitly record why rerun is "
             "impossible or unsafe.",
    )

    p_synth = sub.add_parser(
        "synthesize",
        help="Open emergent-mode synthesis on a scope; all active participants self-select",
    )
    p_synth.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the artifact to synthesize",
    )

    p_take = sub.add_parser(
        "take-role",
        help="Declare that a participant takes the orchestrator role for a scope",
    )
    p_take.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the scope to orchestrate",
    )
    p_take.add_argument(
        "--as",
        dest="as_participant",
        required=True,
        help="Identifier of the participant taking the role",
    )
    p_take.add_argument(
        "--acknowledging",
        default=None,
        help="Optional: entry_id of a release_orchestrator entry being acknowledged. "
             "Use this when the take is the second step of a transfer.",
    )

    p_release = sub.add_parser(
        "release-role",
        help="Declare that a participant releases the orchestrator role for a scope",
    )
    p_release.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the scope to release",
    )
    p_release.add_argument(
        "--as",
        dest="as_participant",
        required=True,
        help="Identifier of the participant releasing the role (must be the current holder)",
    )
    p_release.add_argument(
        "--reason",
        default="voluntary release",
        help="Free-text reason for release",
    )
    p_release.add_argument(
        "--snapshot",
        default=None,
        help="State snapshot for transfer per fnd-participants.md -> Transfer. "
             "String literal, or @path/to/file.md to read from disk.",
    )
    p_release.add_argument(
        "--to",
        dest="transferring_to",
        default=None,
        help="Identifier of the participant the role is being transferred to. "
             "They must acknowledge via take-role --acknowledging.",
    )

    p_self = sub.add_parser(
        "self-select",
        help="A participant declares they are picking up scope from the ledger (Emergent Mode)",
    )
    p_self.add_argument(
        "--scope",
        required=True,
        help="Path (relative to coordination/) of the scope being picked up",
    )
    p_self.add_argument(
        "--as",
        dest="as_participant",
        required=True,
        help="Identifier of the participant self-selecting",
    )
    p_self.add_argument(
        "--reason",
        default=None,
        help="Optional: why this participant is picking up this scope",
    )

    p_ledger = sub.add_parser(
        "ledger",
        help="Print ledger entries (full panels by default; --summary for the compressed view)",
    )
    p_ledger.add_argument(
        "--summary",
        action="store_true",
        help="Print the compressed ledger view per fnd-ledger.md -> Ledger Summary "
             "instead of one panel per entry. Failure/repair/intention_shift/"
             "boundary_change entries are preserved in full; other types are "
             "compressed unless their scope matches --scope.",
    )
    p_ledger.add_argument(
        "--scope",
        default=None,
        help="(--summary only) Treat this scope as active: decision/attempt/"
             "completion entries on it are shown in summary form rather than "
             "compressed to one line.",
    )

    p_inbox = sub.add_parser(
        "inbox",
        help="List or process signal envelopes in signal/inbox/ and signal/archive/",
    )
    p_inbox.add_argument(
        "action",
        choices=["list", "process"],
        help="`list` shows pending and archived signals; `process` dispatches all pending signals via their handlers (offline-friendly — no API calls).",
    )

    args = parser.parse_args()
    if args.cmd == "review":
        return run_review(args.scope, task_type=args.task_type)
    if args.cmd == "repair":
        return run_repair(args.failure_entry, args.arbiter, verify=args.verify)
    if args.cmd == "synthesize":
        return run_synthesis(args.scope)
    if args.cmd == "take-role":
        return cmd_take_role(args.scope, args.as_participant, args.acknowledging)
    if args.cmd == "release-role":
        return cmd_release_role(
            args.scope, args.as_participant, args.reason,
            args.snapshot, args.transferring_to,
        )
    if args.cmd == "self-select":
        return cmd_self_select(args.scope, args.as_participant, args.reason)
    if args.cmd == "ledger":
        if args.summary:
            return print_ledger_summary(args.scope)
        return print_ledger()
    if args.cmd == "inbox":
        if args.action == "list":
            return inbox_list()
        if args.action == "process":
            return inbox_process()
    return 2


if __name__ == "__main__":
    sys.exit(main())
