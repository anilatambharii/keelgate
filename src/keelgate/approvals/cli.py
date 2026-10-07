"""Command-line approval client: ``keelgate-approvals``.

Trust model: the CLI runs as a local operator with filesystem access to the
queue database, and the operator names themself with ``--approver``. That is the
right model for a developer machine and a dev stack, and nothing more; remote or
multi-user approval goes through the REST surface, which authenticates callers.

Evidence text comes from the model. It is printed with control characters
stripped so it cannot drive the operator's terminal.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from keelgate.approvals._models import ApprovalRequest, Approver, sanitize_for_display
from keelgate.approvals._queue import ApprovalError, ApprovalQueue
from keelgate.approvals._tiers import ApprovalTier

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_DB = ".keelgate/approvals.sqlite"


def _render(request: ApprovalRequest, *, full: bool) -> str:
    safe = sanitize_for_display
    lines = [
        f"{request.request_id}  {request.status.value:<9} {request.tier.value:<16} "
        f"tool={safe(request.tool_name)} agent={safe(request.agent_id)}",
    ]
    if full:
        ev = request.evidence
        lines += [
            f"  expires:       {request.expires_at.isoformat()}",
            f"  args:          {safe(json.dumps(ev.args, sort_keys=True))}",
            f"  rationale:     {safe(ev.rationale)}",
            f"  confidence:    {ev.confidence}",
            f"  policy:        {safe(ev.policy_version)}",
            f"  policy reason: {safe('; '.join(ev.policy_reasons)) or '-'}",
            f"  verifier:      {safe('; '.join(ev.verifier_flags)) or '-'}",
        ]
        lines += [f"  source:        {safe(s.uri)}" for s in ev.sources]
        if request.tier is ApprovalTier.EXPLICIT_SIGNOFF:
            lines.append(f"  signoff code:  {request.signoff_code}")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="keelgate-approvals", description=__doc__)
    parser.add_argument(
        "--db",
        default=os.environ.get("KEELGATE_APPROVALS_DB", DEFAULT_DB),
        help=f"approval queue database (default: {DEFAULT_DB})",
    )
    parser.add_argument("--tenant", required=True, help="tenant to operate in")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="list pending requests")
    show = sub.add_parser("show", help="show one request with its evidence")
    show.add_argument("request_id")

    for name in ("approve", "reject"):
        cmd = sub.add_parser(name, help=f"{name} a request")
        cmd.add_argument("request_id")
        cmd.add_argument("--approver", required=True, help="your approver id")
        cmd.add_argument(
            "--max-tier",
            choices=[t.value for t in ApprovalTier if t is not ApprovalTier.AUTO],
            default=ApprovalTier.ONE_CLICK.value,
            help="the highest tier you are cleared to decide",
        )
        cmd.add_argument("--note", default=None)
        if name == "approve":
            cmd.add_argument("--signoff", default=None, help="signoff code (EXPLICIT_SIGNOFF)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``keelgate-approvals`` command line (list, show, approve, reject)."""
    args = _parser().parse_args(argv)
    if args.db != ":memory:":
        Path(args.db).expanduser().parent.mkdir(parents=True, exist_ok=True)
    queue = ApprovalQueue(args.db)
    try:
        if args.command == "list":
            pending = queue.list_pending(args.tenant)
            sys.stdout.write(
                "\n".join(_render(r, full=False) for r in pending) or "no pending requests"
            )
            sys.stdout.write("\n")
        elif args.command == "show":
            sys.stdout.write(_render(queue.get(args.tenant, args.request_id), full=True) + "\n")
        else:
            approver = Approver(
                approver_id=args.approver,
                tenant_id=args.tenant,
                max_tier=ApprovalTier(args.max_tier),
            )
            if args.command == "approve":
                done = queue.approve(
                    args.tenant,
                    args.request_id,
                    approver,
                    signoff_code=args.signoff,
                    note=args.note,
                )
            else:
                done = queue.reject(args.tenant, args.request_id, approver, note=args.note)
            sys.stdout.write(_render(done, full=False) + "\n")
    except ApprovalError as exc:
        sys.stderr.write(f"error [{exc.code}]: {exc}\n")
        return 1
    finally:
        queue.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
