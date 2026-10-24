# Development Log

This log records how the agent was built, tested and changed, step by step, including what
did not work. Each step lists the commits that implement it.

| Date | Step | Summary |
|---|---|---|
| 2026-10-17 | 1 | Agent scaffolding: CLI, inspection tools, agent loop, report contract, tests |
| 2026-10-20 | 2 | Second LLM provider (Google Gemini) and a live smoke test |
| 2026-10-21 | 3 | Baseline evaluation on four public repositories with known defects |
| 2026-10-21 | 4 | Changes made in response to the baseline evaluation |
| 2026-10-21 | 5 | Second evaluation: held-out repositories plus the baseline set; two bugs found and fixed |
| 2026-10-22 | 6 | Gemini made the default provider; Anthropic made optional |
| 2026-10-22 | 7 | Pinned default model, per-error retry handling, automatic model fallback |
| 2026-10-22 | 8 | Third evaluation (default settings, all 7 repositories) with a reusable runner |
| 2026-10-22 | 9 | Scope limited to the nine inspection areas; full Gemini model chain with availability checks |
| 2026-10-23 | 10 | Free-tier model check; probe replaced by local quota counting; Gemma added as last resort |
| 2026-10-23 | 11 | Fourth evaluation (all 7 repositories) after the scope and model-selection changes |
| 2026-10-23 | 12 | Requirements review; one finding per credential, stricter duplicate checks, trace by default |
| 2026-10-23/24 | 13 | Fifth evaluation, after the quota reset and the Step 12 fixes |
| 2026-10-24 | 14 | Fixes from Step 13: rejected findings can't be lost, network retries, narrower permissions and testing rules |
| 2026-10-24 | 15 | `sample_report.json`; sixth evaluation measuring Steps 12–14; redaction fix |

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

## Step 5: Second evaluation

### Setup

Same model (`gemini-flash-lite-latest`) and settings as the baseline (`--max-turns 20`).
The three held-out repositories ran first; their files were read only after the prompt was
frozen, to build answer keys for scoring. The two vulnerable held-out repos are
[gary-mello/github-vuln-actions](https://github.com/gary-mello/github-vuln-actions), whose
README lists 8 planted vulnerabilities, and
[jpr5/vulnerable-workflows-test](https://github.com/jpr5/vulnerable-workflows-test), which
has no list, so 6 defects were identified by reading its 5 workflows. The healthy held-out
repo is [pallets/itsdangerous](https://github.com/pallets/itsdangerous).

Reports and traces are in
[`evals/runs/2026-10-21-after-changes-flash-lite/`](../evals/runs/2026-10-21-after-changes-flash-lite/).

### Results

| Repository | Set | Baseline | After changes |
|---|---|---|---|
| gary-mello/github-vuln-actions | held-out | not run | **0 / 8**. Read all 8 workflows in two parallel turns, then dismissed the repository as "a pure demonstration fixture" |
| jpr5/vulnerable-workflows-test | held-out | not run | **5 / 6, no false positives.** Missed `curl … \| bash` |
| pallets/itsdangerous | held-out, healthy | not run | **0 findings (correct).** A real investigation: read all workflows and configuration, and searched for credentials |
| Plazmaz/leaky-repo | original | 0 / 35 | **14 / 35, no false positives.** The coverage check refused the first `finish_audit`, and the agent then read 7 more files and recorded 6 more findings |
| paastha1/cicd-test-repo | original* | 4 / 6, 2 weak | **6 / 6** as separate findings, plus 2 valid extras (no `permissions:` block, release without tests) |
| Grafter-labs/grafter-lab-injection | original | 1 / 6 | **1 / 6**, plus 1 plausible extra. Still missed `runs-on:` injection and the `contains()` allowlist |
| pypa/sampleproject | original*, healthy | 4 false positives | **0 findings.** The false positives are gone, but the real issue (release without tests) is still missed |

\* Prompt examples were derived from these runs, so their improvement is partly biased (see
Step 4).

**Summary:**
- **Improved:** false positives on the healthy repos dropped to zero, the sensitive-file
  recall on leaky-repo rose from 0 to 14, and the agent now reads files in parallel.
- **Unchanged:** the model still sometimes dismisses fixture-style repositories (gary-mello)
  and misses subtle CI patterns (Grafter), even though both are named explicitly in the
  prompt. The two `intentionally vulnerable` held-out repos gave opposite outcomes on the
  same model, which suggests model capability and run-to-run variance matter as much as the
  prompt.

### Bugs found and fixed during this round

Both affect only logging, not agent behaviour, so they were fixed mid-round. The one run
that crashed was repeated with the fix.

1. **A trace write crashed an audit** (paastha1, exit 1, no report). `_trace` redacted the
   *serialized* JSON of the tool arguments and parsed it back. When the evidence contained
   `password="…"`, the redaction consumed an escaped quote, the JSON became invalid, and
   because the trace writer was outside the tool-error guard, the audit ended.
   - **Fix:** redact each string value inside the structure (`redact_obj`), and wrap trace
     writing so a logging failure can never end an audit.
   - **Regression tests:** `test_trace_survives_quoted_secrets_in_arguments` and
     `test_trace_failure_does_not_end_audit`.
2. **Secret material reached the trace.** With leaky-repo's files now actually opened, a
   scan of the saved traces found a PuTTY private-key header, an Apache `$apr1$` password
   hash and a hex Rails master key in the output previews. These are fake values, but the
   agent must never write secrets to its logs.
   - **Fix:** the redactor masks PuTTY keys and password hashes. Trace previews never store
     file contents: `read_file` keeps only its header line, and `search_code` only its
     `file:line` locations.
   - **Clean-up:** the saved traces for this round were re-processed with the same rule
     before being committed. The baseline traces were scanned and contain no secrets.
   - **Tests:** `test_trace_never_stores_file_contents` and
     `test_redacts_putty_keys_and_password_hashes`.

Test suite: 81 tests, all passing.

### Remaining problems and candidate fixes

1. **Dismissing fixture repositories** (gary-mello). Restate the rule where the decision is
   made: in the first user message and in the `finish_audit` description ("audit it as if it
   were production; demo or fixture status is irrelevant").
2. **Secrets found only by content** (21 of leaky-repo's 35 files have generic names, such as
   `wp-config.php`, `.bashrc`, `heroku.json` and `settings.py`). Instruct a repository-wide
   `search_code` for credential assignments, and add a few more unambiguous filenames
   (`.credentials`, `.bash_history`).
3. **A rejected finding was dropped** (sampleproject). `record_violation` failed on a
   malformed `rule_id`, and `finish_audit` in the same batch ended the run before the model
   could retry. Refuse `finish_audit` in a turn where a `record_violation` failed.
4. **Subtle CI patterns** (Grafter). Prompting alone did not help Flash-Lite. Options are the
   optional `inspect_workflows()` tool, or a stronger model.
5. **Model choice.** All runs so far used Flash-Lite because of free-tier congestion. The
   final `sample_report.json` and a final evaluation should use the strongest available
   model.

---

## Step 6: Gemini as the default provider

**Why:** every run so far used Gemini, and no Anthropic key is available. Until now, though,
the code treated Anthropic as the default: it was chosen when no key was set, it won when both
keys were set, and its SDK was a required dependency. The project should match how it is
actually run and evaluated.

**What changed:**

| Area | Before | After |
|---|---|---|
| Default provider | Anthropic (and Anthropic won if both keys were set) | **Gemini** (Gemini wins if both are set; Anthropic is chosen only when it is the only key, or explicitly) |
| `auditor/llm.py` | Provider selection plus the Claude client | Provider selection and `LLMError` only; each provider is imported only when selected |
| Claude client | Inside `auditor/llm.py` | Moved to `auditor/anthropic_llm.py` |
| `requirements.txt` | `anthropic`, `google-genai`, `python-dotenv` | `google-genai`, `python-dotenv` |
| Anthropic SDK | Required | Optional, in `requirements-anthropic.txt`; installed by `requirements-dev.txt` so tests and CI still cover it |
| `.env.example` | Both keys, Anthropic first | `GEMINI_API_KEY` (with where to get a free key); Anthropic settings in a commented-out optional block |
| README | Both providers side by side | Gemini setup, free-tier notes and Gemini client architecture; Anthropic in one "Optional" section |
| `.gitignore` | — | Ignores `.vscode/` and `.idea/`, since committed local IDE settings are themselves one of the violations the agent checks for |

The agent's behaviour, prompt and tools are unchanged.

**Verification:**
- The suite has 83 tests, all passing. The tests now check that:
  - Gemini is chosen by default, and when both keys are set;
  - the optional Claude path still works when its SDK is installed;
  - choosing Anthropic without its SDK gives a clear error (`pip install -r requirements-anthropic.txt`) instead of an `ImportError`.
- A clean virtual environment with only `requirements.txt` (no `anthropic` package) was
  created to confirm the default install works. The CLI ran, 82 tests passed, and the one
  Claude-only test was skipped.

---

## Step 7: Model selection, retries and fallback

### The problem

A run with the default settings (`gemini-flash-latest`) failed after about 6 minutes with
exit 1, before completing a single turn. Its log showed three separate problems:

| Time | Error | Meaning |
|---|---|---|
| +5 s | `503 UNAVAILABLE`, "high demand" | Google's servers for that model were overloaded |
| +5 min | `504 DEADLINE_EXCEEDED` | The retried request waited in Google's queue until the 300 s timeout ran out |
| +5 min 20 s | `429 RESOURCE_EXHAUSTED`, quota `GenerateRequestsPerDayPerProjectPerModel-FreeTier`, **limit 20**, model `gemini-3.8-flash`, retry in 16 h | The **daily** free quota was used up |

Two findings came out of this:
- **The default was a moving alias.** `gemini-flash-latest` is an alias that Google repoints.
  Its metadata does not say which model it points to; the 429 error revealed it was
  `gemini-3.8-flash`. A free tier of 20 requests a day is fewer than one audit needs.
- **The SDK's retries could not tell errors apart.** They retried a daily-quota 429 (which
  waiting cannot fix) the same way as a brief overload, and waited the full timeout on a
  stuck request.

### Investigating the available models

One minimal request was sent to each text-capable Flash model on the key (2026-10-22):

| Model | Result |
|---|---|
| `gemini-3.5-flash-lite` | OK (but returned 503 at other moments the same day) |
| `gemini-3.6-flash`, `gemini-3-flash-preview` | OK (`gemini-3.5-flash` silently served as `gemini-3.6-flash`) |
| `gemini-3.1-flash-lite` | 503 at the time |
| `gemini-3.7-flash`, `gemini-3.8-flash`, `gemini-flash-latest` | 429 quota |
| `gemini-2.5-flash`, `gemini-2.5-flash-lite` | 404, "no longer available to new users" |

**Can an audit switch models mid-conversation?** Gemini attaches thought signatures to its
turns, so this was tested on the live API. A conversation was started on
`gemini-3.5-flash-lite` (its function call carried a signature) and continued on each other
model:

| Continued on | Turns replayed unchanged | Signatures stripped |
|---|---|---|
| `gemini-3.6-flash` | OK | 400 "Function call is missing a thought_signature" |
| `gemini-3-flash-preview` | OK | 400 |
| `gemini-3.1-flash-lite` | OK | 400 |

So switching is safe as long as turns are replayed exactly as received, which the adapter
already does.

### Changes

| # | Change | Where |
|---|---|---|
| 1 | The default model is the concrete `gemini-3.5-flash-lite` instead of the `gemini-flash-latest` alias, so results are reproducible | `auditor/gemini_llm.py` |
| 2 | Automatic fallback: when a model is unavailable, the audit switches to the next in `AUDIT_FALLBACK_MODELS` (default `gemini-3.1-flash-lite, gemini-3.6-flash, gemini-3-flash-preview`) and stays there. Each model has its own quota. | `auditor/gemini_llm.py` |
| 3 | Retries moved from the SDK into our client, with handling per error type: 5xx is retried twice with backoff and then fails over; a per-minute 429 waits the delay the API requests (up to 60 s); a daily-quota 429, a timeout or a 404 fails over immediately; 400/401/403 stop with a clear error | `auditor/gemini_llm.py` |
| 4 | The request timeout is 120 s instead of 300 s, and a timeout fails over instead of retrying the same model | `auditor/gemini_llm.py` |
| 5 | The concrete model that served the audit (`model_version` from the API) is logged, printed at the end of the run, and written as the last line of the trace | `auditor/gemini_llm.py`, `agent.py` |
| — | If every model fails, a single error lists each model and why it failed (e.g. `daily free-tier quota used up, resets in ~16h`) | `auditor/gemini_llm.py` |

### Verification

- **Tests:** 11 new offline tests use the SDK's real error classes, with error bodies in the
  exact shape of the live 429. They cover each error path, staying on the fallback model,
  the combined error message, and `AUDIT_FALLBACK_MODELS` parsing. The suite has 94 tests,
  all passing.
- **Live run** with no model settings, on `jpr5/vulnerable-workflows-test` with
  `--max-turns 15`: exit 0 in 74 s. On the first turn `gemini-3.5-flash-lite` returned a 503;
  it was retried once after 3 s and succeeded. The run reported
  `model: gemini-3.5-flash-lite`. Results are in
  [`evals/runs/2026-10-22-smoke-defaults/`](../evals/runs/2026-10-22-smoke-defaults/).

### Still open

The same anti-pattern (no `permissions:` block) was recorded once per workflow file, five
times, under a single reused rule ID. The prompt asks for one finding per anti-pattern, so
these are duplicates a grader would count against precision. Because the rule ID is reused,
code can detect it: candidate fix is to merge a repeated rule ID into the first finding's
evidence.

---

## Step 8: Third evaluation, default settings

### Setup

- **New runner:** [`evals/run_eval.py`](../evals/run_eval.py) runs the agent over
  [`evals/repos.txt`](../evals/repos.txt) one repository at a time. It pauses 30 s between
  audits, to stay under the free tier's per-minute limit that caused 429s on Oct 21. It stops
  the batch if no Gemini model can serve a request, and it records each audit's HTTP status
  counts, fallbacks and serving model in `summary.json`.
- **Settings:** the same 7 repositories as Step 5 and `--max-turns 20`. **No model settings
  were set**, so this measures the out-of-the-box defaults from Step 7.
- **Usage before the run:** the API dashboard showed about 155 requests on Oct 21 and about
  65 earlier on Oct 22, most errors being 429s (Oct 21) and 503s (Oct 22).
- **Results:** reports, traces and the summary are in
  [`evals/runs/2026-10-22-full-defaults/`](../evals/runs/2026-10-22-full-defaults/).

### Reliability

All 7 audits finished with exit 0, in 38–228 s each. The batch used **76 requests: 62 OK,
13 × 503, 1 × 429**, and every error was absorbed by retries or fallback.

- **Live fallback:** on paastha1, `gemini-3.5-flash-lite` returned 503 three times and the
  audit switched to `gemini-3.1-flash-lite`. That returned 503 three times too, so it switched
  to `gemini-3.6-flash`, which served the rest of the audit. A per-minute 429 there asked for
  a 52 s wait, which was honoured.
- **Switching models mid-audit** caused no errors, as the live test in Step 7 predicted.

### Detection

| Repository | Set | Step 5 | Step 8 |
|---|---|---|---|
| gary-mello/github-vuln-actions | held-out | 0 / 8 | **6 / 8**, no false positives (missed `@main`/`@latest` refs and the self-hosted runner) |
| jpr5/vulnerable-workflows-test | held-out | 5 / 6 | 4 / 6, plus the same "missing permissions" finding 4 extra times |
| pallets/itsdangerous | held-out, healthy | 0 findings | 0 findings (correct) |
| paastha1/cicd-test-repo | tuned | 6 / 6 + 2 valid | 6 / 6 + 2 valid |
| Grafter-labs/grafter-lab-injection | tuned | 1 / 6 | 1 / 6 |
| Plazmaz/leaky-repo | tuned | 14 / 35 | **1 / 35** (one valid hygiene finding) |
| pypa/sampleproject | tuned, healthy | 0 findings | 0 findings (the real release-without-tests issue is still missed) |

### Analysis

- **Healthy repositories:** precision held. Both produced zero findings in both rounds.
- **Run-to-run variance is large.** The same code and model gave 0 → 6 on gary-mello but
  14 → 1 on leaky-repo. A single run per repository is not a reliable measurement.
- **Why leaky-repo regressed** (from its trace):
  - Turns 2–17 were spent one call at a time on `list_files` pattern searches (`*Cargo.*`,
    `*go.*`, `*pom.xml`…) instead of opening the files tagged `[sensitive name]`.
  - The model again called the repository "a test fixture and benchmark".
  - The coverage check refused the first `finish_audit`, but only at turn 18 of 20, too late
    to act on: the model opened one file and finished.
- **Duplicates:** jpr5 repeated "missing permissions block" in 5 findings with *different*
  rule IDs but identical descriptions. Merging by rule ID (proposed in Step 7) would not
  catch this; merging by identical description would.
- **Grafter's subtle patterns** (`runs-on:` injection, the `contains()` allowlist, deprecated
  commands) were missed in all three rounds.

### Candidate fixes, in priority order

1. **Mid-audit coverage reminder.** When half the turns are used and tagged or workflow files
   are still unread, append the list to the next tool result, instead of only checking at
   `finish_audit`.
2. **Merge duplicate findings.** A finding with the same category and description as an
   existing one is folded into it, with its location added to the evidence.
3. **Repeat the no-dismissal rule where the decision is made**, in the first user message and
   in the `finish_audit` description.
4. **Grafter-style CI patterns:** an `inspect_workflows()` tool, or a stronger model.
5. **Measure with repeats:** run each repository two or three times and compare averages,
   within the free-tier quota.

---

## Step 9: Assignment scope and the full model chain

### 1. Audit scope limited to the nine inspection areas

**Why:** the prompt's checklist had grown beyond the assignment and also left parts of it out:
- **Beyond the assignment:** deprecated workflow commands, action pinning (supply-chain
  hardening) and debug-mode flags. None of these is among the nine DevOps inspection areas
  in section 4.
- **Missing:** a missing README, linting/formatting, incorrect CI triggers, inconsistent or
  weakly pinned dependencies, absent lockfiles and review guidance.

**Changes:**
- **Prompt** (`auditor/prompts.py`): the system prompt is now organised by the nine areas.
  Each area states the assignment's example violations word for word, then the concrete signs
  to check. An explicit out-of-scope rule forbids reporting anything outside the nine areas,
  naming general bugs, style, licensing, action pinning, deprecated syntax and ML concerns.
  The assignment's evidence rule is included as written. Tension between the "not a
  violation" list and the assignment was resolved in the assignment's favour: review guidance
  is a finding only "where relevant".
- **Validation** (`auditor/report.py`): every category maps to one rule-ID prefix
  (`security` → `SEC`, `collaboration` → `COL`, …). `record_violation` rejects a finding whose
  prefix doesn't match its category, so every finding is filed under exactly one of the nine
  areas.
- **Tools** (`tools.py`): the "debug flags" hint was removed from the `search_code` description.

**Tests:** one test checks that the prompt contains each area's example violations from the
assignment and each category-to-prefix mapping. Another checks that mismatched prefixes are
rejected.

### 2. A full model chain, most capable first, with availability checks

**Why:** the fallback list was a hand-picked set of four models, and it started with a
Flash-Lite model. Models were only found to be unusable by sending them real requests.

**Changes** (`auditor/gemini_llm.py`):

| Change | Detail |
|---|---|
| Chain built automatically | The API's model list (a metadata call that uses no quota) is filtered to concrete general-purpose text models and ranked **from most to least capable**: Pro, then Flash, then Flash-Lite; newest version first; stable before preview. On the development key that is 12 models, from `gemini-3.1-pro-preview` to `gemini-2.5-flash-lite`. A built-in copy is used if listing fails. |
| Check before use: free | Daily-quota exhaustion, retirement and recent overloads are recorded with an expiry time in a small JSON file (`~/.cache/devops-audit-agent/gemini-model-status.json`, configurable with `AUDIT_MODEL_STATUS_FILE`). A recorded model is skipped **without sending any request**. |
| Check before use: one request | Otherwise a tiny probe (`ping`, at most 16 output tokens) is sent before the model's first real request in an audit. |
| Attempts | 2 attempts per model instead of 3. |
| 504 handling | A 504 (server deadline expired, about 2 minutes) moves on at once instead of retrying. This was found in the live run below, where two retried 504s cost about 4 minutes. |
| Configuration | `AUDIT_MODEL` puts one model first; `AUDIT_FALLBACK_MODELS` replaces the discovered chain. |

**Live run** (`jpr5/vulnerable-workflows-test`, default settings, `--max-turns 15`): exit 0.
The chain behaved as designed. The two Pro models were unavailable (daily quota; retired) and
were recorded. `gemini-3.8-flash` timed out, and `gemini-3.7-flash` was out of quota.
`gemini-3.6-flash` passed its check and served until its daily quota ran out mid-audit.
`gemini-3-flash-preview` timed out, `gemini-2.5-flash` was retired, and
`gemini-3.5-flash-lite` served the rest. The run took 507 s, of which about 4 minutes were
the two retried 504s that led to the 504 change above; that change has not yet been run live.
The report used the new categories (e.g. `COL-` for unsafe PR automation) and recorded
"missing permissions" once instead of once per workflow.

**Tests:** new tests cover:
- the ranking (against the real model list from the development key)
- discovery, and its fallback to the built-in chain
- skipping a recorded model without any request
- the record persisting between runs and expiring
- probing each model once
- a rejected probe
- a 504 moving on without a retry

The suite has 103 tests, all passing.

---

## Step 10: What the free tier offers, and choosing a model without test requests

**Why:** the Step 9 probe had two problems:
- **It cost quota and saved nothing.** If the model worked, the probe was an extra request;
  if it did not, the real request would have failed just as quickly. On Flash models, with
  about 20 requests a day, each probe used 5% of the day's quota.
- **It could not prevent mid-audit failures.** An audit could start on a model with two
  requests left and be forced to switch partway through.

Nothing reachable with an API key reports remaining quota, and 503 overloads cannot be
predicted. So the aim was to predict what can be predicted (quota) and make the rest cheap.

### 1. Free-tier model check (2026-10-23, about 23:00 Pacific)

**Method:** a scratch script sent one request with one tool declaration to each of the 20
general-purpose text models the key lists. It recorded the status, the served
`model_version`, whether the model called the tool, and the quota details of any 429.

Skipped:
- image, speech, music, embedding, live and deep-research models, which cannot run the agent
- the `-latest` aliases, which point to models already checked

Raw results: `evals/model-checks/2026-10-23.json`.

| Result | Models | Interpretation |
|---|---|---|
| Served, tool call made | `gemini-3.8-flash`, `3.6-flash`, `3-flash-preview`, `3.5-flash-lite`, `3.1-flash-lite`, `gemma-4-26b-a4b-it`, `gemini-robotics-er-2-preview` | Usable (robotics model excluded as specialised) |
| Served as another model | `gemini-3.1-flash-lite-preview` → `gemini-3.1-flash-lite` | Not a separate fallback |
| 429, daily quota, limit 20 | `gemini-3.7-flash`, `3.5-flash` | Spent today; usable after the reset |
| 429, every quota exceeded on the first request, no limit stated | `gemini-4-argon`, `3.1-pro-preview`, `3.1-pro-preview-customtools`, `omni-1.1-flash`, `omni-flash-preview` | Not on the free tier (Google's pricing page agrees) |
| 404 "no longer available to new users" | `gemini-2.5-pro`, `2.5-flash`, `2.5-flash-lite` | Free only for projects that already used them |
| 500, twice | `gemma-4-31b-it` | Broken at the time of the check |
| 400 "Function calling is not enabled" | `antigravity-preview-09-2026` | Cannot run the agent |

**Research:**
- Google's rate-limit documentation gives no free-tier figures. It refers to the AI Studio
  rate-limit page and states that daily quotas reset at midnight Pacific.
- Third-party trackers disagree. The most recent ones report about 20 requests a day for the
  3.5–3.8 Flash models (matching the check) and about 500 for Flash-Lite. Older figures of
  1,500 a day predate cuts made around December 2025.
- Gemma's free limits are reported to be much higher, but no current source confirms them.

**Two observations not fully explained:**
- The daily-quota error for `gemini-3.7-flash` names `gemini-3.8-flash` in its quota
  dimension, and the one for `3.5-flash` names `3.6-flash`. Yet `3.8-flash` and `3.6-flash`
  kept serving afterwards, so they are not a shared pool.
- Step 9's run recorded `3.6-flash` as out of daily quota on the same Pacific day on which it
  then served twice.

So quota errors cannot be fully predicted from the agent's own records. A real quota error
remains the final word.

**Gemma compatibility:**
- A fresh request with a system prompt and tools worked, and Gemma called the tool.
- A conversation started on Gemini and handed to Gemma failed with
  `400 Request contains an invalid argument`.
- The same turns without Gemini's thought signatures were accepted.

### 2. Changes

**Changes** (`auditor/gemini_llm.py`):

| Change | Detail |
|---|---|
| Probe removed | The first real request is the check. A failed request moves to the next model with the same conversation, so nothing is lost. |
| Local request counting | Successful requests are counted per model per quota day (midnight Pacific; `tzdata` added for Windows). A model's daily limit is learned from the `quotaValue` of the daily-quota error that states it. Together these show how much quota a model has left **without sending a request**. Stored in the same status file, whose old format is still read. |
| Quota reserve | A model is chosen only if it has at least 10 requests left today (or an unknown amount). If no model has that many, the most capable one with any quota left is used. Once a model serves an audit it is kept while it works, even below the reserve. Audits in the Step 8 evaluation made 5–20 successful requests (median 6). |
| Not on the free tier | A 429 that reports every free-tier quota exceeded, with no limit stated, is recorded as "not available on the free tier" for 7 days. Detection is by error shape, not model name, so Pro returns automatically if billing is enabled. |
| Previews that redirect | A preview is dropped from the chain when its stable release is listed. |
| Gemma added | `gemma-<version>-<size>b-it` models are ranked after Flash-Lite, larger first. When Gemma serves, model turns are replayed without thought parts and signatures. Gemini still gets them unchanged. |
| Unsupported features | A 400 saying a feature is "not enabled" or "not supported" moves to the next model; other 400s still stop the audit. |
| Built-in chain | Used only if listing fails. It now holds the 9 models that served on the free tier in the check, instead of 12 including unusable ones. |

**Effect on the discovered chain:**
- Pro and the 2.5 series are still in the chain.
- Pro is recorded for 7 days after its first error, as are the retired 2.5 models, so each
  costs one request a week instead of one a day.
- `3.1-flash-lite-preview` is gone.
- `gemma-4-31b-it` and `gemma-4-26b-a4b-it` are the last resort.
- Gemma's audit quality is untested.

**Tests:** the probe tests were replaced with tests covering:
- the ranking against the 2026-10-23 model list, including Gemma and the dropped preview
- no extra requests beyond the real ones
- free-tier detection and its 7-day record
- learning a limit and counting locally
- passing over a model below the reserve, but using it when nothing better remains
- staying on the serving model below the reserve
- the midnight-Pacific reset
- reading the old status file
- unsupported features moving on
- Gemma receiving history without signatures while Gemini gets it unchanged

The suite has 112 tests, all passing.

---

## Step 11: Fourth evaluation, after the scope and model-selection changes

### Setup

- **Runner and settings:** the same as Step 8. `evals/run_eval.py` ran over the same 7
  repositories with `--max-turns 20`, a 30 s pause between audits, and no model settings.
- **Starting state:** the run started about 40 minutes before the midnight-Pacific quota
  reset. The status file recorded Pro and every Flash model above `3-flash-preview` as spent
  or retired. This exercised the new selection logic but meant the strongest models were not
  available.
- **Results:** reports, traces, logs and the summary are in
  [`evals/runs/2026-10-23-quota-aware/`](../evals/runs/2026-10-23-quota-aware/). A scan of
  every file found no secrets and no local paths.

### Reliability

All 7 audits exited 0, in 16–72 s each: **237 s in total, against 664 s in Step 8**. The
batch used **111 requests: 102 OK, 9 × 429, no 503s**.

- **No requests were sent to known-unavailable models.** Pro, the retired 2.5 models and the
  spent Flash models were skipped from the status file in every audit.
- **`gemini-3-flash-preview` was used up during the run.** It served the first two audits'
  opening turns until a daily-quota 429 stated its limit (20). That limit was recorded, and
  later audits skipped the model without a request.
- **Per-minute limits caused three of the four fallbacks.** On `3.5-flash-lite` the API asked
  for a 3–6 s wait; the retry was also rate-limited, so after 2 attempts the audit moved to
  `3.1-flash-lite`.
- **Serving models:** mostly `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite`, the same
  tier as Steps 5 and 8. So the detection changes below mostly reflect the Step 9 prompt, not
  a stronger model.

### Detection

Since Step 9, action pinning (gary-mello) and deprecated workflow commands (Grafter) are out
of scope. They are left out of the answer keys below.

| Repository | Set | Step 8 | Step 11 |
|---|---|---|---|
| gary-mello/github-vuln-actions | held-out | 6 / 8 | **6 / 7 in scope**, plus 2 valid extras (auto-merge under `pull_request_target` without review; push-to-main deploy without tests), no false positives. The three expression injections are one finding with the other two files in its evidence. Still misses the self-hosted runner. |
| jpr5/vulnerable-workflows-test | held-out | 4 / 6 + 4 duplicates | **4 / 6, no duplicates.** "Missing permissions" is reported once, naming all five workflows. Misses the `inputs.*` injection in `manual-deploy.yml` and `curl … \| bash`. |
| pallets/itsdangerous | held-out, healthy | 0 | **0 (correct)** |
| paastha1/cicd-test-repo | tuned | 6 / 6 + 2 | **6 / 6**, plus 5 plausible extras (deploy without tests, no `permissions:`, `npm ci` without a lockfile, `npm test` with no tests, no linting). Two duplicates: "missing `.gitignore`" twice under the same rule ID, and the hardcoded production password from SEC-001 again as CFG-001. |
| Grafter-labs/grafter-lab-injection | tuned | 1 / 6 | **3 / 5 in scope**: script injection; **`runs-on:` injection, missed in every earlier round**; and untrusted `head.ref` in a download URL. Plus 1 valid extra (`contents: write` on an `issue_comment` trigger). One false positive: `npm publish --dry-run` flagged as a release without tests. Still misses the substring `contains()` allowlist and the echoed secret-shaped literal. |
| Plazmaz/leaky-repo | tuned | 1 / 35 | **3 findings naming 9 / 35 files.** Two are separate (`.ssh/id_rsa`, `etc/shadow`); the third lists 7 credential files in its evidence. |
| pypa/sampleproject | tuned, healthy | 0 | **1 borderline finding:** `test.yml` has no `permissions:` block (`release.yml` has one). Release-without-tests is still missed. |

### Analysis

- **In-scope recall improved on the subtle CI repositories.** On Grafter it rose from 1 to 3,
  on the same model tier. Duplicates on jpr5 are gone. Both match the Step 9 prompt changes:
  each area lists its concrete signs, and missing permissions is reported once per repository.
- **The healthy repositories stayed clean or nearly clean.** itsdangerous had no findings.
  sampleproject had one: the missing `permissions:` block, which the prompt treats as a
  violation. That is defensible, but it costs precision on a "mostly healthy" repository.
- **Grouping now costs recall on leaky-repo.** The rule "record the same anti-pattern once,
  with the other locations in the evidence" made the model fold seven different committed
  credential files into one finding. Each committed credential is its own violation, with its
  own rotation step, and a grader would most likely count them separately.
- **Duplicates still slip through** when the location differs: paastha1's "missing
  `.gitignore`" was recorded once with `file: null` and once at `.gitignore:1`. The duplicate
  check compares `file:line`, so it did not catch this.
- **Caveat:** this is one run per repository, and Steps 5 and 8 showed large run-to-run
  variance.

### Candidate fixes, in priority order

1. **One finding per committed credential file.** State in the granularity rules that each
   committed secret or credential file is a separate violation, and that grouping applies to
   repeated configuration patterns (such as missing permissions), not to secrets.
2. **Reject a second finding with the same rule ID and description**, whatever its location
   (catches paastha1's double `.gitignore`).
3. **Per-minute 429s:** when the API asks for only a few seconds, wait it out rather than
   spending one of the 2 attempts, so a short rate limit does not push the audit onto a weaker
   model.
4. Still open from Step 8: a mid-audit coverage reminder, and repeating the no-dismissal rule
   at `finish_audit`.
5. **Rerun after the quota reset**, so the Flash models serve, and with repeats, to separate
   the model's effect from the prompt's.

---

## Step 12: Requirements review and the Step 11 fixes

### Requirements review

Every item in the assignment was checked against the repository:

| Section | Requirement | Status |
|---|---|---|
| 2 | Public URL in, LLM-directed tool selection with follow-ups, unattended, target unchanged | Met |
| 3 | `list_files`, `read_file`, `search_code`, `git_log`; hand-written loop; no agent frameworks | Met |
| 4 | Nine inspection areas; evidence rule; no inferring branch protection | Met (Step 9 prompt) |
| 5 | Exact command; valid JSON, including an empty array; non-zero exit on fatal errors; all fields; allowed severities; `null` file/line; no secret values | Met |
| 6 | "Tool-call traces may be inspected" | **Gap:** a trace was only written with `--trace`, and the evaluator runs the bare command |
| 8 | `agent.py`, `tools.py`, agent modules, `requirements.txt`, `.env.example`, `README.md`, `tests/` | Met |
| 8 | `sample_report.json`: authentic example output | **Missing:** to be produced from a final run |
| 9 | "Do not invent results when a check is unavailable" | **Partly:** stated for tests only |

### Changes

| # | Change | Why | Where |
|---|---|---|---|
| 1 | Each committed credential file and each hardcoded credential is its own violation; grouping is only for repeated configuration patterns. Committed credentials are always `security`, not `repository_hygiene`. | Step 11: leaky-repo's seven credential files were folded into one `HYG-001` finding. Each credential must be revoked and rotated separately, and a grader counts each one. | `auditor/prompts.py` |
| 2 | `record_violation` rejects the same category and description again when it is in the same file or one of the two has no file, and names the earlier finding. | Step 11: paastha1 recorded "missing `.gitignore`" twice, once with `file: null` and once at `.gitignore:1`. The `file:line` check could not see it. "Duplicate findings earn no extra credit." | `auditor/report.py` (`same_problem`), `auditor/loop.py` |
| 3 | `record_violation` rejects a `rule_id` that is already in use and gives the next free one (e.g. `CI-002`). | The prompt already required sequential numbering, but reused IDs (paastha1's two `HYG-001`, Step 7's five reused IDs) went through. | `auditor/report.py` (`next_rule_id`), `auditor/loop.py` |
| 4 | Explicit rule: never invent results; when a check is unavailable (tests, builds or the application cannot be run, or a tool call failed), record nothing that depends on it. | Assignment section 9. | `auditor/prompts.py` |
| 5 | A trace is written by default next to the report (`report.json` → `report.trace.jsonl`). `--trace FILE` chooses the path; `--no-trace` turns it off. | Assignment section 6: the evaluator runs the bare command, and a trace now exists for them to inspect. | `agent.py`, `.gitignore` |

The same description in two **different** files is still accepted, so that identical credentials
committed in several files are each reported (change 1).

**Tests:** 4 new tests cover:
- the paastha1 duplicate
- identical descriptions in different files being allowed
- a reused rule ID being rejected with the next free one
- the default trace path, `--trace` and `--no-trace`

The suite has 116 tests, all passing.

These changes affect detection. They have not been evaluated live yet; the next evaluation
round measures them.

---

## Step 13: Fifth evaluation, after the quota reset and the Step 12 fixes

### Setup

- **Runner and settings:** the same as Steps 8 and 11. All 7 repositories, `--max-turns 20`,
  no model settings.
- **Timing:** the round started at 09:11 Pacific on Oct 23, after the daily quota reset, so the
  Flash models were available again.
- **Results:** in
  [`evals/runs/2026-10-23-flash-after-reset/`](../evals/runs/2026-10-23-flash-after-reset/).
  `summary.json` merges the two attempts described below. A scan found no secrets: its one
  hit was a search pattern the model typed (`BEGIN PRIVATE KEY`). No local paths appear in
  any file.

### Reliability

- **First attempt: 4 audits succeeded, then the machine's network dropped.**
  - Grafter and leaky-repo ended with `Could not reach the Gemini API: [WinError 10054] An
    existing connection was forcibly closed by the remote host`.
  - sampleproject ended with `Could not resolve host: github.com`.
  - All three exited 1 with a clear message, as specified. Their logs and traces are kept as
    `*.attempt1-network-failure.*`, and `summary-attempt1.json` holds the original summary.
- **Retry:** the three were rerun the next morning (09:23–09:41 Pacific, Oct 24) and all exited 0.
- **Total for the 7 successful audits:** 111 requests (98 OK, 6 × 429, 3 × 503, 4 × 504).
- **The stronger models were mostly unavailable in practice.**
  - `3.8-flash` and `3.7-flash` returned 504 (server timed out after about 2 minutes) three
    times each.
  - `3.6-flash` reported its daily quota used up after only a few requests, the third time
    its quota has not matched our count (see Step 10).
  - So only gary-mello and jpr5 ran mainly on Flash models (`3.6-flash`, `3-flash-preview`).
    The other five ended up on `3.5-flash-lite`.
- **The quota reserve worked as designed.** After two audits, `3-flash-preview` had 8
  requests left, below the reserve of 10. It was passed over without a request.

### Detection

| Repository | Step 11 | Step 13 | Serving model |
|---|---|---|---|
| gary-mello (held-out) | 6 / 7 + 2 extras | 6 / 7, 10 findings. Extras: auto-merge without review, deploy without tests, `npm ci` without a lockfile, `npm test` with no `package.json`, no `.gitignore`, no review guidance next to auto-merge. All are defensible, but they add volume. Still misses the self-hosted runner. | 3.6-flash → 3-flash-preview |
| jpr5 (held-out) | 4 / 6 | **5 / 6, the best so far.** It caught the `inputs.*` injection in `manual-deploy.yml` for the first time and still misses `curl … \| bash`. Extras: no real tests (dummy test script), CI never builds, no `.gitignore`, no linter. | 3-flash-preview |
| itsdangerous (healthy) | 0 | **1 false positive:** missing `permissions:` block | 3.5-flash-lite |
| paastha1 | 6 / 6 + 5 extras + 2 duplicates | **6 / 6, 10 findings, no duplicates.** Each hardcoded credential is now its own finding (4), as Step 12 intended. | 3.5-flash-lite |
| Grafter | 3 / 5 | **0: lost to a loop flaw** (below) | 3.8-flash → 3.5-flash-lite |
| leaky-repo | 9 / 35 named in 3 findings | **16 / 35, one finding per file, no false positives.** The best so far (Step 5: 14). | 3.5-flash-lite |
| sampleproject (healthy) | 1 borderline | **2 false positives:** missing `permissions:` block, and a "fragile" `os.listdir` index in `noxfile.py` (a code nitpick filed under testing) | 3.5-flash-lite |

### Analysis

1. **The Step 12 credential rule worked.** leaky-repo went from 3 grouped findings to 16
   separate ones, and paastha1 reported each of its 4 hardcoded credentials separately. Both
   ran on Flash-Lite, so this is the prompt's effect, not a stronger model's.
2. **The duplicate checks worked.** paastha1 had no duplicates, and no audit reused a rule ID.
3. **Grafter was lost to a loop flaw, not a detection miss.**
   - The audit read every workflow on `3.8-flash`.
   - It then spent turns 6–11 on broad searches, at 30–120 s per turn.
   - After two 504s it was switched to Flash-Lite. On turn 13 that model sent two
     `record_violation` calls with garbled arguments (`rule_id: "COL-001,severity:"`).
   - Both were rejected, but `finish_audit` **in the same turn** ended the run before the model
     could correct them. Step 5 listed exactly this as a candidate fix, and this time it
     cost the whole repository.
4. **Missing `permissions:` blocks cause false positives on healthy repositories.**
   - It was reported on itsdangerous here and on sampleproject in Steps 11 and 13.
   - Whether a missing block is dangerous depends on the repository's default token setting,
     which is a remote setting the assignment says not to infer.
   - But it is also a planted defect in gary-mello (`no-permissions-declared.yml`), so
     dropping it would cost recall there.
5. **A dropped network connection ends the audit at once.** `httpx` connection errors are
   treated as fatal, so one reset connection cost two audits that a retry would likely have
   saved.

### Candidate fixes

1. Refuse `finish_audit` in a turn in which a `record_violation` was rejected, at most twice,
   so the model can resubmit its findings.
2. Retry connection resets and DNS failures with backoff before giving up. They are network
   problems, not model problems, so no model is recorded as unavailable.
3. Missing `permissions:` block: decide between reporting it always (recall on planted
   defects) and only where a trigger or step makes the default token risky (precision on
   healthy repositories).
4. Testing findings must be about missing, unrun or demonstrably broken tests, not the style
   of test helper code.

---

## Step 14: Fixes from the fifth evaluation

| # | Change | Step 13 evidence | Where |
|---|---|---|---|
| 1 | `finish_audit` is refused in a turn in which a `record_violation` was rejected. The refusal says how many were rejected and asks for them to be corrected and resubmitted. It happens at most twice per audit and never on the last turn, so the model still has the final say. | Grafter: two garbled findings (`rule_id: "COL-001,severity:"`) were rejected, and the `finish_audit` in the same turn ended the audit with 0 findings | `auditor/loop.py` |
| 2 | Network errors (`httpx.TransportError`: connection reset, DNS failure, …) are retried on the same model up to 5 times, waiting about 2, 4, 8 and 16 s. They do not count as one of the model's 2 attempts, and no model is recorded as unavailable. Timeouts keep their existing handling (the request reached a congested model). | First attempt: `[WinError 10054] … forcibly closed by the remote host` ended Grafter and leaky-repo immediately | `auditor/gemini_llm.py` (`_send`) |
| 2 | `git clone` is retried twice (after 5 s, then 15 s) when git's error is a network error ("Could not resolve host", "Connection reset", …). Other failures, such as a repository that does not exist, are still reported at once. | First attempt: `Could not resolve host: github.com` ended sampleproject | `auditor/repo.py` |
| 3 | A missing `permissions:` block is reported only where the default token is risky: triggers carrying untrusted input (pull_request_target, issues, issue_comment, workflow_run) or steps that push, publish, deploy, merge, comment or label. A missing block on an ordinary build/test workflow is listed under "not violations". The repository's default token permission is a remote setting the assignment says not to infer. | False positive on itsdangerous (Step 13) and sampleproject (Steps 11, 13) | `auditor/prompts.py` |
| 4 | Testing findings are about whether tests exist, run in CI and can pass, not the style of test or helper code. Small code nitpicks are listed under "not violations". | sampleproject: an `os.listdir` index in `noxfile.py` was reported as `TEST-001` | `auditor/prompts.py` |

**Trade-off in change 3:** gary-mello's planted `no-permissions-declared.yml` is a push/PR
build workflow, so this finding will probably no longer be reported there. That costs one
point of recall on a repository that planted it, in exchange for not reporting it on healthy
repositories. The hidden type-C repository is there specifically to test false positives.

**Tests:** 7 new tests cover:
- finish refused after a rejected finding, then accepted after resubmission
- that refusal being bounded
- a network error retried on the same model without blocking it
- a network that stays down giving one clear error, with no model blamed
- clone retry on a DNS error, no retry for a missing repository, and giving up after three
  network failures

The suite has 123 tests, all passing.

### Bug found by CI: clone cleanup crashed on Python 3.11

The Step 14 push failed CI on Python 3.11. The 3.13 job was cancelled automatically when
3.11 failed.

- **Symptom:** the three new clone tests failed with
  `TypeError: rmtree() got an unexpected keyword argument 'onexc'`.
- **Cause:** `cleanup()` in `auditor/repo.py` passed `onexc=` to `shutil.rmtree`, and that
  argument only exists from Python 3.12. The bug dates from Step 1. It never showed because:
  - no test had called `cleanup()` before;
  - every live run used the development machine's Python 3.14.
- **Impact:** `agent.py` calls `cleanup()` after writing the report. On Python 3.11, which the
  README lists as supported, every audit would have written its report and then exited 1 with
  a traceback.
- **Fix:**
  - `onexc=` on 3.12+, `onerror=` on 3.11; the callback is the same.
  - `cleanup()` never raises. A clone that cannot be deleted is logged and left in the temp
    directory, because the report is already written.
- **Tests:** cleanup of a read-only clone under both code paths (3.11 and 3.12+), and a
  failing delete being logged instead of raised. A ruff check targeting Python 3.11
  (`--target-version py311`) was also run over all source files, including
  `evals/run_eval.py`, and found nothing.

The suite has 126 tests, all passing locally; CI re-checks on 3.11 and 3.13.

**Follow-up (CI, Step 15 push):**
- **Result:** on Python 3.11, 131 of 132 tests passed, including the real clone and cleanup
  tests, so the fix works.
- **The one failure was the new test itself.** It faked "Python 3.13" on a 3.11 interpreter,
  which forced the `onexc=` path that 3.11 does not have.
- **Fix:** the test now runs the interpreter's own path, plus a faked 3.11 path. `onerror=`
  exists on every supported version, so faking 3.11 is safe anywhere; faking a newer version
  on an older interpreter is not.

---

## Step 15: Sample report and sixth evaluation

### `sample_report.json`

- **Command:** run right after the quota reset (12:02 PKT, Oct 24), with the exact command
  from the assignment and default settings:
  `python agent.py --repo https://github.com/jpr5/vulnerable-workflows-test --output sample_report.json`.
- **Repository:** `jpr5/vulnerable-workflows-test` was chosen because it is held out (not
  used to tune the prompt), small, and has defects in several areas.
- **Models:** the audit started on `gemini-3.8-flash`, which returned 503 twice.
  `3.7-flash` timed out (504), `3.6-flash` was rate-limited, and `3.5-flash` reported its
  daily quota spent. It finished on `gemini-3-flash-preview`. 32 requests in all; exit 0.
- **Result:** 10 findings covering 5 of the 6 known defects (all but `curl … | bash`):
  - the `pull_request_target` checkout of the PR head;
  - untrusted input in `run:`, with `greet-pr.yml` and `manual-deploy.yml` in one finding;
  - the hardcoded API key, shown only as `[REDACTED]`;
  - missing `permissions:` blocks, limited by the Step 14 rule to the three risky workflows,
    with the reason stated.

  The other findings are defensible extras: CI does not run on pull requests, CI never builds,
  a deploy has no test gate, the test script is a dummy, there is no `.gitignore`, and there is
  no linter.
- **Trace:** `sample_report.trace.jsonl` (24 lines) is committed alongside. Both files were
  scanned and contain no secrets and no local paths. The report is unedited.

### Sixth evaluation (Steps 12–14)

**Setup:** the same runner and settings as Steps 8, 11 and 13, started right after the sample
report. Results are in
[`evals/runs/2026-10-24-steps-12-14/`](../evals/runs/2026-10-24-steps-12-14/). A scan found
no secrets and no local paths.

**Reliability:**
- All 7 audits exited 0. The round used 96 requests: 86 OK, 6 × 429, 3 × 503, 1 × 504.
- **The sample report had used most of the day's Flash quota first.** In the round, only
  gary-mello ran on Flash (`3.6-flash`, then `3-flash-preview`).
- itsdangerous tried `3.8-flash` (503) and `3.7-flash` (504) before falling back.
- The other five audits ran on `3.5-flash-lite`.

**The Step 14 refusal worked twice:**
- **Grafter:** three findings were rejected (missing `severity`). The `finish_audit` in the
  same turn was refused, and the model resubmitted all three. Step 13 lost all of Grafter to
  exactly this.
- **sampleproject:** the same happened with one finding.

The Step 4 coverage check also fired on leaky-repo, which then opened the files it had skipped.

| Repository | Step 11 | Step 13 | Step 15 | Model |
|---|---|---|---|---|
| gary-mello (held-out) | 6 / 7 | 6 / 7 | 5 / 7 + 6 extras | 3.6-flash → 3-flash-preview |
| jpr5 (held-out) | 4 / 6 | 5 / 6 | 3 / 6, no false positives | 3.5-flash-lite |
| itsdangerous (healthy) | 0 | 1 false positive | **0 (correct)** | 3.8 → 3.5-flash-lite |
| paastha1 | 6 / 6 + 2 duplicates | 6 / 6 | 6 / 6, 13 findings | 3.5-flash-lite |
| Grafter | 3 / 5 | 0 (lost) | 1 / 5 + 2 valid extras | 3.5-flash-lite |
| leaky-repo | 9 / 35 | 16 / 35 | 15 / 35 + 1 valid extra | 3.5-flash-lite |
| sampleproject (healthy) | 1 borderline | 2 false positives | 1 false positive | 3.5-flash-lite |

Notes on the scores:
- **gary-mello:** as predicted in Step 14, the planted `no-permissions-declared.yml` (a
  push/PR build workflow) is no longer reported. Missing permissions *is* reported for the
  workflows with untrusted triggers.
- **jpr5:** a Flash-Lite run that stopped after 5 requests. On Flash (the sample report),
  the same repository scored 5 / 6.
- **paastha1:** of the 7 findings beyond the 6 patterns, 4 are additional hardcoded
  credentials (each its own violation). The other 3 repeat an anti-pattern already reported in
  another file (echo, `curl -v`, artifact upload). A grader may count those 3 as duplicates.
  The audit reached the 20-turn limit.
- **Grafter:** a short Flash-Lite run (8 requests). It found script injection, write
  permissions on an `issue_comment` trigger, and a `curl -v` with an `Authorization` header in
  `test.yaml` that earlier rounds missed. It missed `runs-on:` injection, the `contains()`
  allowlist, the download-name collision and the echoed secret-shaped literal.
- **sampleproject:** one false positive. `release.yml`'s `permissions: id-token: write` was
  called insufficient, although it is the documented minimal setting for trusted publishing.

### Analysis

- **Healthy repositories improved** (Step 14 permissions and testing rules). itsdangerous was
  clean again, and sampleproject went from 2 false positives to 1.
- **The Step 12 credential rule holds** on leaky-repo (15–16 separate findings in both rounds
  since) and on paastha1.
- **Model capability now dominates recall on the subtle repositories.** On Flash-Lite, jpr5
  and Grafter finished after 5–8 requests with fewer findings. On Flash, the same repositories
  scored 5 / 6 (sample report) and 3 / 5 (Step 11, partly Flash). The free tier gives about
  20 requests per Flash model a day, so a full round cannot run on Flash. An evaluator with
  their own key and quota would get the stronger models.

### Bug found: over-redaction made evidence unreadable

- **Symptom:** sampleproject's evidence read `permissions block with only id-token: [REDACTED]`.
- **Cause:** the generic `key: value` redaction masked any value after a key containing
  "token", including the permission level `write`. It also swallowed the word `Bearer` in
  `Authorization: Bearer $TOKEN`.
- **Fix** (`auditor/redact.py`):
  - Values that cannot be secrets are kept: permission levels (`read`, `write`, `none`),
    booleans, and plain variable references such as `$TOKEN`.
  - The credential after `Bearer`/`Basic` is masked on its own, so the scheme word is kept and
    the secret still is not.
- **Tests:** 6 new tests. One of them caught a gap in the first version of the fix: keeping
  `Bearer` would have left a literal bearer token unmasked. The final version masks it.
- The suite has 132 tests, all passing.
