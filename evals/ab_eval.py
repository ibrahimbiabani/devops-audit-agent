"""A/B evaluation: baseline code vs new code, same pinned model, runs interleaved per repo.

Usage: python ab_eval.py <baseline_root> <new_root> <repos.txt> <out_dir> <runs>
Each audit: agent.py --repo R --output ... --trace ... --max-turns 20 -v, with
AUDIT_MODEL / AUDIT_FALLBACK_MODELS from the environment (both arms identical).
"""

import collections
import json
import re
import subprocess
import sys
import time
from pathlib import Path

HTTP = re.compile(r'models/([\w.-]+):generateContent "HTTP/1\.1 (\d{3})')

baseline, new, repos_file, out, runs = (
    Path(sys.argv[1]),
    Path(sys.argv[2]),
    Path(sys.argv[3]),
    Path(sys.argv[4]),
    int(sys.argv[5]),
)
out = out.resolve()  # each arm runs with its own code folder as the working directory
only = set(sys.argv[6].split(",")) if len(sys.argv) > 6 else None  # e.g. "Plazmaz__leaky-repo.run2"
repos = [line.split()[0] for line in repos_file.read_text().splitlines() if line.startswith("http")]
arms = {"baseline": baseline, "new": new}
if len(sys.argv) > 7:  # e.g. "new-fixed": run only the new code, under that arm name
    arms = {sys.argv[7]: new}
results = []
for url in repos:
    name = "__".join(url.rstrip("/").split("/")[-2:])
    for run in range(1, runs + 1):
        if only and f"{name}.run{run}" not in only:
            continue
        for arm, root in arms.items():
            d = out / arm
            d.mkdir(parents=True, exist_ok=True)
            stem = d / f"{name}.run{run}"
            cmd = [
                sys.executable,
                "-W",
                "ignore",
                str(root / "agent.py"),
                "--repo",
                url,
                "--output",
                f"{stem}.json",
                "--trace",
                f"{stem}.trace.jsonl",
                "--max-turns",
                "20",
                "-v",
            ]
            started = time.monotonic()
            with open(f"{stem}.log", "w", encoding="utf-8") as fh:
                code = subprocess.run(cmd, cwd=root, stdout=fh, stderr=subprocess.STDOUT).returncode
            log = Path(f"{stem}.log").read_text(encoding="utf-8", errors="replace")
            http = collections.Counter(f"{m}:{s}" for m, s in HTTP.findall(log))
            r = {
                "repo": name,
                "arm": arm,
                "run": run,
                "exit": code,
                "seconds": round(time.monotonic() - started),
                "http": dict(sorted(http.items())),
            }
            if code == 0:
                report = json.loads(Path(f"{stem}.json").read_text(encoding="utf-8"))
                r["violations"] = [
                    f"{v['rule_id']} {v['file']}:{v['line']} {v['description'][:70]}" for v in report["violations"]
                ]
                trace = [json.loads(x) for x in Path(f"{stem}.trace.jsonl").read_text(encoding="utf-8").splitlines()]
                r["tools"] = dict(collections.Counter(e["tool"] for e in trace if "tool" in e))
                r["models"] = next((e["models"] for e in trace if e.get("event") == "models_used"), [])
                # Findings recorded after the final-review request (new arm only).
                review_turn = next(
                    (
                        e["turn"]
                        for e in trace
                        if e.get("tool") == "finish_audit"
                        and e.get("is_error")
                        and "Final review" in e.get("output_preview", "")
                    ),
                    None,
                )
                r["recorded_after_review"] = sum(
                    1
                    for e in trace
                    if review_turn
                    and e.get("tool") == "record_violation"
                    and not e.get("is_error")
                    and e["turn"] > review_turn
                )
            else:
                r["error"] = next((x for x in log.splitlines() if x.startswith("error:")), "see log")
            results.append(r)
            print(
                json.dumps(
                    {k: r[k] for k in ("repo", "arm", "run", "exit", "seconds")}
                    | {
                        "n": len(r.get("violations", [])),
                        "after_review": r.get("recorded_after_review"),
                        "models": r.get("models"),
                    }
                ),
                flush=True,
            )
            (out / "summary.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
            time.sleep(10)
