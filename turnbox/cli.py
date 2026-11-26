"""Command line interface for operating the turnbox service."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any

from .service import TurnBoxService, sample_service


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def service_for(args: argparse.Namespace, *, writable: bool = False) -> TurnBoxService:
    db = getattr(args, "db", None) or os.environ.get("TURNBOX_DB")
    if db:
        return TurnBoxService.load(db)
    default_db = Path("data/turnbox.json")
    if default_db.exists():
        return TurnBoxService.load(default_db)
    if writable:
        svc = TurnBoxService()
        svc.state_path = default_db
        # Trial convenience: operational commands work on the bundled synthetic
        # exchange until a real --db is initialized.  Explicit import commands
        # still start empty and ingest exactly what they are given.
        if getattr(args, "command", "") not in {"load-dir", "ingest-file"}:
            samples = Path(__file__).resolve().parents[1] / "samples"
            if samples.exists():
                svc.load_directory(samples)
        return svc
    return sample_service()


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", help="JSON state database (or set TURNBOX_DB)")
    parser = argparse.ArgumentParser(prog="turnbox", description="联营周转餐盒租次、点验、退押和门店往来服务", parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run HTTP/JSON service", parents=[common])
    p_serve.add_argument("--host", default=os.environ.get("HOST", "0.0.0.0"))
    p_serve.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    p_serve.add_argument("--load-samples", action="store_true")

    p_init = sub.add_parser("init-samples", help="load samples into --db", parents=[common])
    p_init.add_argument("--samples", default=str(Path(__file__).resolve().parents[1] / "samples"))

    p_load = sub.add_parser("load-dir", help="load NDJSON exchange directory", parents=[common])
    p_load.add_argument("directory")

    p_ingest = sub.add_parser("ingest-file", help="ingest one NDJSON file", parents=[common])
    p_ingest.add_argument("file")
    p_ingest.add_argument("--type")

    for name, help_text in [
        ("customer", "顾客视图"),
        ("merchant", "门店/商户视图"),
        ("store", "门店视图别名"),
        ("point", "还盒点/机器点视图"),
        ("box", "实物追踪"),
        ("issue", "租次/退款业务视图"),
    ]:
        p = sub.add_parser(name, help=help_text, parents=[common])
        p.add_argument("id")
        p.add_argument("--period")

    sub.add_parser("disputes", help="列出争议", parents=[common])
    sub.add_parser("admin", help="管理端总览", parents=[common])
    sub.add_parser("integrity", help="资料关联完整性检查", parents=[common])

    p_settle = sub.add_parser("settle", help="生成/定稿月度服务费结算", parents=[common])
    p_settle.add_argument("period", help="YYYY-MM")
    p_settle.add_argument("--draft", action="store_true")
    p_settle.add_argument("--at")

    p_resolve = sub.add_parser("resolve-dispute", help="管理员核实争议", parents=[common])
    p_resolve.add_argument("dispute")
    p_resolve.add_argument("--uphold", help="claim ref, e.g. hot-d/c1")
    p_resolve.add_argument("--reject-all", action="store_true")
    p_resolve.add_argument("--by", default="admin")
    p_resolve.add_argument("--at")
    p_resolve.add_argument("--note", default="")

    p_refund = sub.add_parser("settle-refund", help="记录收付方退款到账确认", parents=[common])
    p_refund.add_argument("channel_ref")
    p_refund.add_argument("--at")
    p_refund.add_argument("--amount", type=int)

    return parser


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if not argv:
        argv = ["serve"]
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        from .server import serve

        db = args.db or os.environ.get("TURNBOX_DB") or "data/turnbox.json"
        load_samples = bool(args.load_samples or os.environ.get("TURNBOX_LOAD_SAMPLES") == "1")
        if not Path(db).exists() and os.environ.get("TURNBOX_NO_AUTO_SAMPLES") != "1":
            load_samples = True
        serve(args.host, args.port, db, load_samples)
        return 0

    if args.command == "init-samples":
        svc = service_for(args, writable=True)
        result = svc.load_directory(args.samples)
        path = svc.save(args.db)
        print_json({"loaded": result, "db": str(path)})
        return 0

    svc = service_for(args, writable=args.command in {"load-dir", "ingest-file", "settle", "resolve-dispute", "settle-refund"})

    if args.command == "load-dir":
        result = svc.load_directory(args.directory)
        svc.save(args.db)
        print_json(result)
    elif args.command == "ingest-file":
        result = svc.ingest_file(args.file, args.type)
        svc.save(args.db)
        print_json(result)
    elif args.command == "customer":
        print_json(svc.customer_view(args.id))
    elif args.command in {"merchant", "store"}:
        print_json(svc.merchant_view(args.id, args.period))
    elif args.command == "point":
        print_json(svc.point_view(args.id))
    elif args.command == "box":
        print_json(svc.box_view(args.id))
    elif args.command == "issue":
        print_json(svc.issue_view(args.id))
    elif args.command == "disputes":
        print_json({"disputes": svc.list_disputes()})
    elif args.command == "admin":
        print_json(svc.admin_overview())
    elif args.command == "integrity":
        print_json(svc.integrity_report())
    elif args.command == "settle":
        result = svc.create_settlement(
            args.period,
            {"finalize": not args.draft, "at": args.at, "finalized_at": args.at},
        )
        svc.save(args.db)
        print_json(result)
    elif args.command == "resolve-dispute":
        if not args.uphold and not args.reject_all:
            parser.error("resolve-dispute requires --uphold point/event or --reject-all")
        payload = {
            "decision": "reject_all" if args.reject_all else "uphold",
            "claim": args.uphold,
            "resolved_by": args.by,
            "at": args.at,
            "resolved_at": args.at,
            "note": args.note,
        }
        result = svc.resolve_dispute(args.dispute, payload)
        svc.save(args.db)
        print_json(result)
    elif args.command == "settle-refund":
        result = svc.transition_refund(
            args.channel_ref,
            "settled",
            {"occurred_at": args.at, "amount": args.amount} if args.amount is not None else {"occurred_at": args.at},
        )
        svc.save(args.db)
        print_json(result)
    else:  # pragma: no cover
        parser.error(f"unknown command {args.command}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
