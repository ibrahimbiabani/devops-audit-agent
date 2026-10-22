"""CLI entry point.

python agent.py --repo https://github.com/<owner>/<repo> --output report.json
"""

from __future__ import annotations

import argparse
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
    p.add_argument("--trace", type=Path, help="Write a JSONL tool-call trace to this file")
    p.add_argument("--provider", choices=PROVIDERS, help="LLM provider (default: gemini; or LLM_PROVIDER)")
    p.add_argument("--model", help="Override the model (default: AUDIT_MODEL, else gemini-flash-latest)")
    p.add_argument("--keep-clone", action="store_true", help="Do not delete the temporary clone")
    p.add_argument("-v", "--verbose", action="store_true", help="Log agent steps to stderr")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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

    if args.trace:
        args.trace.parent.mkdir(parents=True, exist_ok=True)
        args.trace.write_text("", encoding="utf-8")

    workdir = None
    try:
        llm = make_llm(args.provider, args.model)
        log.info("Using %s model %s", type(llm).__name__, llm.model)
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

    print(f"Wrote {len(violations)} violation(s) to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
