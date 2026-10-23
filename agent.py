"""CLI entry point.

python agent.py --repo https://github.com/<owner>/<repo> --output report.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

from auditor.llm import PROVIDERS, LLMError, make_llm
from auditor.loop import AgentError, AuditAgent
from auditor.repo import RepoError, cleanup, clone, normalize_github_url
from auditor.report import build_report, write_report
from tools import RepoTools, ToolError

log = logging.getLogger("agent")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AI agent that audits a GitHub repository for DevOps violations.")
    p.add_argument("--repo", required=True, help="Public GitHub repository URL")
    p.add_argument("--output", required=True, type=Path, help="Where to write the JSON report")
    p.add_argument("--max-turns", type=int, default=40, help="Upper bound on agent turns (default: 40)")
    p.add_argument(
        "--trace",
        type=Path,
        help="Tool-call trace file (JSONL, redacted; default: <output>.trace.jsonl next to the report)",
    )
    p.add_argument("--no-trace", action="store_true", help="Do not write a tool-call trace")
    p.add_argument("--provider", choices=PROVIDERS, help="LLM provider (default: gemini; or LLM_PROVIDER)")
    p.add_argument("--model", help="Try this model first (default: AUDIT_MODEL, else the most capable available)")
    p.add_argument("--keep-clone", action="store_true", help="Do not delete the temporary clone")
    p.add_argument("-v", "--verbose", action="store_true", help="Log agent steps to stderr")
    return p.parse_args(argv)


def trace_path(args: argparse.Namespace) -> Path | None:
    """Where the tool-call trace goes: --trace, else next to the report (report.json -> report.trace.jsonl)."""
    if args.no_trace:
        return None
    return args.trace or args.output.with_name(f"{args.output.stem}.trace.jsonl")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    args.trace = trace_path(args)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    load_dotenv()

    try:
        repository = normalize_github_url(args.repo)
    except RepoError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.trace:  # started fresh on each run
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        args.trace.write_text("", encoding="utf-8")

    workdir = None
    try:
        llm = make_llm(args.provider, args.model)
        log.info("Using %s, models in order: %s", type(llm).__name__, ", ".join(getattr(llm, "models", [llm.model])))
        log.info("Cloning %s", repository)
        workdir = clone(repository)
        agent = AuditAgent(llm, RepoTools(workdir), max_turns=args.max_turns, trace_path=args.trace)
        violations = agent.run(repository)
        write_report(build_report(repository, violations), args.output)
    except (RepoError, ToolError, LLMError, AgentError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("error: interrupted", file=sys.stderr)
        return 130
    finally:
        if workdir and not args.keep_clone:
            cleanup(workdir)
        elif workdir:
            print(f"clone kept at {workdir}", file=sys.stderr)

    used = getattr(llm, "models_used", None) or [llm.model]
    if args.trace:
        with args.trace.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "models_used", "models": used}) + "\n")
    print(f"Wrote {len(violations)} violation(s) to {args.output} (model: {', '.join(used)})", file=sys.stderr)
    if args.trace:
        print(f"Tool-call trace: {args.trace}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
