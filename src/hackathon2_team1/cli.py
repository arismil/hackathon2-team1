"""Command line entry point: `nfs-agent <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import PROJECT_DIR, get_settings
from .observability import setup_logging
from .pdf import write_pdf

DEFAULT_REQUEST = PROJECT_DIR / "evaluation" / "requests" / "asteria.json"


def _print_progress(snap: dict) -> None:
    v = snap["values"]
    plan = v.get("plan") or {}
    print(f"\n=== {snap['assessment_id']}  status={snap['status']}")
    for t in plan.get("tasks", []):
        print(f"  [{t['status']:>9}] {t['task_id']:<4} {t['assigned_agent']:<24} {len(t['required_checks'])} checks")
    if v.get("decision"):
        d = v["decision"]
        print(f"  recommendation={d['recommendation']}  overall_risk={d['overall_risk']}  "
              f"human_review_required={d['human_review_required']}")
        for o in d.get("overrides", []):
            print(f"  guardrail override: {o}")
    if v.get("quality"):
        print("  quality:", json.dumps(v["quality"]))
    for p in snap.get("run", {}).get("phases", []):
        tok = sum(u.get("total_tokens", 0) for u in p["tokens"].values())
        print(f"  phase {p['phase']}: {p['seconds']}s, {tok} tokens, trace={p['trace_id']}, error={p['error']}")


async def _assess(args) -> int:
    from .schemas import VendorAssessmentRequest
    from .service import AssessmentService

    req = VendorAssessmentRequest.model_validate_json(Path(args.request).read_text())
    svc = AssessmentService()
    snap = await svc.start(req)
    _print_progress(snap)
    aid = snap["assessment_id"]
    while snap["pending_review"]:
        pr = snap["pending_review"]
        print("\n--- HUMAN REVIEW REQUIRED ---")
        print(json.dumps(pr, indent=2))
        if args.decision:
            human = {"decision": args.decision, "approver": args.approver, "role": args.role, "comments": "CLI"}
            args.decision = None  # one scripted attempt, then interactive/exit
        elif sys.stdin.isatty() and not args.no_input:
            print(f"Draft report: {Path(args.out) / (aid + '-draft.md')}")
            Path(args.out).mkdir(parents=True, exist_ok=True)
            (Path(args.out) / f"{aid}-draft.md").write_text(snap["values"].get("report_markdown", ""))
            human = {
                "decision": input(f"Decision {pr['allowed_decisions']}: ").strip(),
                "approver": input("Approver name: ").strip(),
                "role": input(f"Your role (required: {pr['required_role']}): ").strip(),
                "comments": input("Comments: ").strip(),
            }
        else:
            print("Stopping at human review (use --decision/--approver/--role or run interactively).")
            break
        snap = await svc.resume(aid, human)
        _print_progress(snap)
        for a in snap["values"].get("review_attempts", [])[-1:]:
            if not a["accepted"]:
                print(f"  review attempt rejected: {a['reason']}")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{aid}.md").write_text(snap["values"].get("report_markdown", ""))
    (out / f"{aid}.json").write_text(json.dumps(snap, indent=2, default=str))
    write_pdf(snap["values"].get("report_markdown", ""), out / f"{aid}.pdf", f"Vendor Assessment - {req.vendor_name}")
    print(f"\nReport: {out / (aid + '.md')} (PDF: {out / (aid + '.pdf')})")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="nfs-agent", description="NFS Vendor Risk & Procurement Deep Agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    ing = sub.add_parser("ingest", help="build the ChromaDB index from the knowledge corpus")
    ing.add_argument("--rebuild", action="store_true")
    sub.add_parser("mcp", help="run the NFS MCP server (streamable HTTP)")
    api = sub.add_parser("api", help="run the FastAPI app")
    api.add_argument("--port", type=int, default=8000)
    a = sub.add_parser("assess", help="run an assessment end-to-end")
    a.add_argument("--request", default=str(DEFAULT_REQUEST))
    a.add_argument("--decision", choices=["APPROVE", "CONDITIONAL APPROVAL", "REJECT"])
    a.add_argument("--approver", default="cli-user")
    a.add_argument("--role", default="executive_risk_owner")
    a.add_argument("--no-input", action="store_true")
    a.add_argument("--out", default=str(PROJECT_DIR / "data" / "reports"))
    args = p.parse_args()
    setup_logging()

    if args.cmd == "ingest":
        from .rag import KnowledgeStore

        n = KnowledgeStore().ingest(rebuild=args.rebuild)
        print(f"indexed {n} chunks into {get_settings().chroma_dir}")
    elif args.cmd == "mcp":
        from .mcp_server import main as mcp_main

        sys.argv = [sys.argv[0]]
        mcp_main()
    elif args.cmd == "api":
        import uvicorn

        uvicorn.run("hackathon2_team1.api:app", host="0.0.0.0", port=args.port)
    elif args.cmd == "assess":
        sys.exit(asyncio.run(_assess(args)))


if __name__ == "__main__":
    main()
