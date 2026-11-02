"""Run a single agent against the configured database, mailbox and LLM.

Manual testing helper. It never bypasses the approval gate: the `send` action
only mails drafts that HR has already approved.

Examples:
    python scripts/run_agent.py inbox
    python scripts/run_agent.py screen --run-id <uuid>
    python scripts/run_agent.py send --live --yes
"""

from __future__ import annotations

import argparse
import json
import sys

from app.agents.ceo_report_agent import generate_report
from app.agents.contracts import AgentContext, AgentResult
from app.agents.hr_agent import (
    draft_decisions,
    forward_candidates,
    read_inbox,
    send_approved,
)
from app.agents.hr_manager_agent import build_shortlist_file, screen_forwarded
from app.core.config import get_settings
from app.core.exceptions import RunAlreadyActiveError
from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.schemas.domain import Actor
from app.services.workflow_service import WorkflowService
from app.utils.imap_client import ImapClient
from app.utils.llm_factory import get_llm
from app.utils.mailer import Mailer

ACTIONS = {
    "inbox": read_inbox,
    "forward": forward_candidates,
    "draft": draft_decisions,
    "send": send_approved,
    "screen": screen_forwarded,
    "shortlist": build_shortlist_file,
    "report": generate_report,
}


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one HR agent step.")
    parser.add_argument("agent", choices=sorted(ACTIONS))
    parser.add_argument("--run-id", help="Attach to an existing workflow run.")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Actually send mail (default is the configured dry-run mode).",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Required together with --live for the send action.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)

    if args.live and not args.yes:
        print("Refusing to send live mail without --yes.", file=sys.stderr)
        return 2

    if args.live:
        settings = settings.model_copy(update={"email_dry_run": False})

    with SessionLocal() as session:
        workflows = WorkflowService(session)
        if args.run_id:
            run = workflows.get(args.run_id)
        else:
            try:
                run = workflows.start(actor=Actor.agent("script"))
            except RunAlreadyActiveError:
                active = workflows.active_run()
                print(
                    f"Another run is active ({active.id if active else 'unknown'}); "
                    "pass --run-id or finish it first.",
                    file=sys.stderr,
                )
                return 3

        ctx = AgentContext.build(
            session=session,
            llm=get_llm(),
            mailer=Mailer(settings),
            imap=ImapClient(settings),
            settings=settings,
            actor=Actor.agent(f"script:{args.agent}"),
            run_id=run.id,
            dry_run=not args.live,
        )
        result: AgentResult = ACTIONS[args.agent](ctx)

    print(
        json.dumps(
            {
                "run_id": run.id,
                "agent": args.agent,
                "counts": result.counts,
                "errors": result.errors,
                "stop": result.stop,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
