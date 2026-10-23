"""Run the agent over a list of repositories, one at a time, and summarise the run.

    python evals/run_eval.py --repos evals/repos.txt --out evals/runs/<date>-<label>

Audits run sequentially with a pause between them, so the free tier's
per-minute limits are not hit. The batch stops early if an audit fails because
no Gemini model could serve it, instead of using up more quota. For each audit
it records the exit code, duration, serving model, findings, and the HTTP
status of every API request (from the agent's -v log).
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTTP_STATUS = re.compile(r'HTTP Request: POST \S+ "HTTP/[\d.]+ (\d{3})')
EXHAUSTED = "No Gemini model could serve the request"


def read_repos(path: Path) -> list[dict[str, str]]:
    repos = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            url, *rest = line.split()
            repos.append({"url": url, "type": rest[0] if rest else "", "set": rest[1] if len(rest) > 1 else ""})
    return repos


def audit(repo: dict[str, str], out: Path, max_turns: int) -> dict:
    name = "__".join(repo["url"].rstrip("/").split("/")[-2:])
    report, trace, logfile = out / f"{name}.json", out / f"{name}.trace.jsonl", out / f"{name}.log"
    cmd = [sys.executable, "-W", "ignore", str(ROOT / "agent.py"), "--repo", repo["url"], "--output", str(report)]
    cmd += ["--trace", str(trace), "--max-turns", str(max_turns), "-v"]
    started = time.monotonic()
    with logfile.open("w", encoding="utf-8") as fh:
        code = subprocess.run(cmd, cwd=ROOT, stdout=fh, stderr=subprocess.STDOUT).returncode
    seconds = round(time.monotonic() - started)
    log = logfile.read_text(encoding="utf-8", errors="replace")

    result = {**repo, "name": name, "exit": code, "seconds": seconds}
    result["http"] = dict(sorted(collections.Counter(HTTP_STATUS.findall(log)).items()))
    result["fallbacks"] = log.count("switching to")
    result["exhausted"] = EXHAUSTED in log
    if code == 0:
        result["violations"] = len(json.loads(report.read_text(encoding="utf-8"))["violations"])
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines() if '"event"' in line]
        result["models"] = events[-1]["models"] if events else []
    else:
        result["error"] = next((line for line in log.splitlines() if line.startswith("error:")), "see log")
    return result


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repos", type=Path, default=ROOT / "evals" / "repos.txt")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--max-turns", type=int, default=20)
    p.add_argument("--pause", type=int, default=30, help="Seconds between audits (default 30)")
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    results = []
    repos = read_repos(args.repos)
    for i, repo in enumerate(repos):
        if i:
            time.sleep(args.pause)
        r = audit(repo, args.out, args.max_turns)
        results.append(r)
        print(json.dumps(r), flush=True)
        if r["exhausted"]:
            print(f"Stopping: no Gemini model available; {len(repos) - i - 1} repo(s) not run.", flush=True)
            break

    totals = collections.Counter()
    for r in results:
        totals.update(r["http"])
    summary = {"results": results, "http_totals": dict(sorted(totals.items())), "requests": sum(totals.values())}
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"requests": summary["requests"], "http_totals": summary["http_totals"]}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
