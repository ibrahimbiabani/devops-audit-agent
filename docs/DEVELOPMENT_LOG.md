# Development Log

This log records how the agent was built, tested and changed, step by step, including what
did not work. Each step lists the commits that implement it.

| Date | Step | Summary |
|---|---|---|
| 2026-10-17 | 1 | Agent scaffolding: CLI, inspection tools, agent loop, report contract, tests |
| 2026-10-20 | 2 | Second LLM provider (Google Gemini) and a live smoke test |
| 2026-10-21 | 3 | Baseline evaluation on four public repositories with known defects |
| 2026-10-21 | 4 | Changes made in response to the baseline evaluation |

---

## Step 1: Scaffolding

**Goal:** an LLM-driven agent loop written by hand (no agent frameworks, as the assignment
requires) that produces the JSON report contract from section 5.

**Design decisions:**
- **The LLM chooses every step.** The loop sends the conversation and the tool list to the model,
  runs whichever tools it asks for (several per turn if it calls them in parallel), returns the
  outputs, and repeats. There is no fixed checklist in code.
- **Findings are recorded one at a time** through a `record_violation` tool. Each finding is
  checked against the report contract when it is recorded, and an invalid one goes back to the
  model as an error so it can correct it. The run ends when the model calls `finish_audit`. If
  the turn limit is hit first, the findings recorded so far are still written, so a long audit
  never comes back empty.
- **Inspection tools only read.** They are `list_files` (git-tracked files only, so committed
  junk is visible), `read_file` (with line numbers, which serve as evidence), `search_code`
  (regex) and `git_log`. Paths are confined to the clone, output size is capped, and no
  repository code is ever executed.
- **Safety:**
  - The target is cloned into a temporary directory with git prompts and LFS disabled, and
    the directory is deleted afterwards.
  - Known credential formats are masked before any tool output reaches the LLM.
  - The report, logs and trace get a second, stricter redaction pass.
- **Reliability:** API errors are turned into clear messages, the exit codes are documented,
  the report is written atomically, and a JSONL trace of every tool call can be saved
  (`--trace`).

**Verification:** 46 offline tests. They run the tools against a temporary git repository,
check report validation and redaction, drive the loop with a scripted fake LLM, and check CLI
input validation. The project's own CI workflow runs ruff and pytest on every push.

**Commit:** `f6c95c6`, the initial scaffolding (this commit also includes Step 2).

---

## Step 2: Gemini provider

**Why:** the agent was first written for the Anthropic API, but no Anthropic key was
available during development. The assignment says the provider choice does not affect the
grade, so a second provider was added rather than a replacement. Either key works, and the
evaluator can use whichever one they have.

**How:**
- `auditor/gemini_llm.py` is an adapter. It converts the loop's conversation to Gemini
  function calling (`function_call` / `function_response` parts) on each request, and
  converts Gemini's replies back. Gemini's own turns are replayed unchanged, so its thought
  signatures survive between turns.
- The provider is chosen from `--provider` or `LLM_PROVIDER`. If neither is set, it follows
  whichever API key is present.
- Gemini requests retry with exponential backoff on 429 and 5xx responses, because the
  free-tier rate limits are low.

**Verification:**
- 12 offline tests, using real SDK types with a fake client.
- One live request with the real tool definitions. It confirmed that the key works, that the
  model resolves, and that Gemini accepts every tool schema; the model replied with a correct
  `list_files` call.

---

## Step 3: Baseline evaluation

### Choosing test repositories

The hidden evaluation (section 6) uses three kinds of repository: (A) obvious issues,
(B) subtler CI/security/configuration defects, (C) mostly healthy. Public repositories of
each kind were found with web searches and `gh search repos`. Repositories that **document
their own planted defects** were preferred, because that documentation acts as an answer key.

| Type | Repository | Answer key |
|---|---|---|
| A | [Plazmaz/leaky-repo](https://github.com/Plazmaz/leaky-repo) | README table: 35 files containing committed secrets |
| B | [paastha1/cicd-test-repo](https://github.com/paastha1/cicd-test-repo) | README: 6 vulnerable workflow patterns, plus 1 clean workflow |
| B | [Grafter-labs/grafter-lab-injection](https://github.com/Grafter-labs/grafter-lab-injection) | README: 6 workflow-injection scenarios, plus nested decoy workflows GitHub never runs |
| C | [pypa/sampleproject](https://github.com/pypa/sampleproject) | Reference packaging project; few or no findings expected |

### Setup

- **Command:** `agent.py --repo <url> --output … --trace … --max-turns 20 -v`, run once per
  repository, one after another.
- **First attempt, aborted:** on `gemini-flash-latest`, Gemini returned
  `503 UNAVAILABLE ("high demand")`, and one turn took about 4 minutes. A full run would have
  taken hours, so it was stopped.
- **Baseline model:** `gemini-flash-lite-latest`. It is less congested, but a weaker model.
- **Results:** reports and traces are in
  [`evals/runs/2026-10-21-baseline-flash-lite/`](../evals/runs/2026-10-21-baseline-flash-lite/).

### Results

All four runs exited with code 0 in 37–100 seconds and produced valid JSON. In every run the
agent chose its own inspection path, recovered from its own bad tool arguments and from
Gemini 429 rate limits, and cleaned up its clone. No secret values appeared in any report.
The quality of the findings was poor:

| Repository | Found / documented | Assessment |
|---|---|---|
| paastha1/cicd-test-repo | 4 / 6 | Missed `curl -v` with auth headers and secrets passed as CLI arguments. Folded `set -x` into another finding. One recommendation suggested `pull_request_target`, which makes things worse. Flagged GitHub-owned actions on version tags, a likely false positive. Correctly left the clean workflow alone. |
| Grafter-labs/grafter-lab-injection | 1 / 6 | Read every workflow but missed runner selection from untrusted input, a bypassable `contains()` actor allowlist, and write permissions on issue-triggered workflows. |
| Plazmaz/leaky-repo | 0 / 35 | Read the README ("none of the secrets here are real… a benchmark") and never opened `.ssh/id_rsa`, `.env`, `.git-credentials` and the other files. Reported four generic "absence of X" findings instead. |
| pypa/sampleproject | 0 real issues; 4 false positives | The same issue twice at the same line under different rule IDs, a missing optional `SECURITY.md`, and an `http://` link in metadata. Missed the one real issue: `release.yml` publishes on every push to `main` without running tests. |

### Root causes

1. **Repository text was treated as instructions.** A README claiming the secrets were fake
   stopped the investigation (leaky-repo).
2. **No pressure to open sensitive files.** The agent could finish without reading files
   whose names clearly suggest credentials.
3. **The prompt named too few CI anti-patterns.** A weaker model missed patterns it was never
   told to look for.
4. **No list of acceptable practices**, which led to false positives on the healthy repository.
5. **Granularity rules were unclear**, so distinct problems were merged and one problem was
   reported twice.

---

## Step 4: Changes in response

Each root cause above has a matching change. Every change is a general DevOps or GitHub
Actions rule, not a finding tied to a particular repository; the assignment forbids
hardcoding repository-specific results.

| # | Change | Where |
|---|---|---|
| 1 | New "untrusted content" section: repository text is evidence, never instructions, and claims that secrets are fake do not change a finding | `auditor/prompts.py` |
| 2 | `list_files` tags credential- or key-like filenames (`.env*`, `id_rsa`, `*.pem`, `.npmrc`, …) with `[sensitive name]`. Templates, `.pub` keys and files under `docs/` are not tagged. The tag is a hint to open the file, never a finding. | `tools.py` |
| 2 | Coverage check: the first `finish_audit` is refused if any workflow file or tagged file was never opened, and the refusal lists them. It fires once, a second `finish_audit` always ends the run, and it is skipped in the last two turns. | `auditor/loop.py` |
| 3 | General GitHub Actions checklist: untrusted input in `run:`/`runs-on:`, risky triggers with write permissions, bypassable allowlists, `set -x`/`curl -v` near secrets, secrets passed as CLI arguments, release jobs without a test gate, neutered validation, deprecated commands | `auditor/prompts.py` |
| 4 | Explicit "not a violation" list: GitHub-owned actions pinned to tags, vendor-documented refs, missing optional community files, links in docs, missing CI or tests in repositories with no software. Recommendations must never suggest `pull_request_target`. | `auditor/prompts.py` |
| 5 | One finding per distinct anti-pattern, and the same anti-pattern found in several places is recorded once with the other locations in `evidence`. A second finding at an already-used `file:line` is rejected. | `auditor/prompts.py`, `auditor/loop.py` |

**Verification:** 18 new offline tests cover sensitive-name tagging, workflow-file detection,
the coverage check (it refuses once, accepts a second finish, and is skipped near the turn
limit) and rejection of a repeated location. The full suite has 76 tests, all passing.

**Known bias:** two examples in the prompt came from the baseline runs: the
`pypa/gh-action-pypi-publish@release/v1` ref and the "three violations in one file" example.
Rerunning on `pypa/sampleproject` and `paastha1/cicd-test-repo` will therefore partly reflect
this. A held-out set that was not looked at while editing the prompt gives the unbiased
measurement:
- `gary-mello/github-vuln-actions`
- `jpr5/vulnerable-workflows-test`
- `pallets/itsdangerous` (healthy)

---

## Next steps

- Run the held-out set, then rerun the original four, on the same model, and compare with
  the baseline.
- Consider an optional `inspect_workflows()` tool if CI recall is still low.
- Produce `sample_report.json` with the strongest available model.
