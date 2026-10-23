# DevOps Repository Auditing Agent

An LLM agent that clones a public GitHub repository, investigates it with read-only inspection
tools, and writes a JSON report of DevOps best-practice violations with evidence and remediation.

The agent loop, tool dispatch and control flow are hand-written Python. The LLM is Google
Gemini, called through Google's official `google-genai` SDK. No agent frameworks are used.
(Anthropic Claude is supported as an optional alternative; see
[Optional: Anthropic Claude](#optional-anthropic-claude).)

## Installation

Requires Python 3.11+ and `git` on `PATH`.

```bash
git clone <this-repo-url>
cd <this-repo>
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Setup

```bash
cp .env.example .env
```

Set your Gemini API key in `.env` (or export it in the shell):

```bash
GEMINI_API_KEY=your-key-here
```

A free key is available from [Google AI Studio](https://aistudio.google.com/apikey).
`GOOGLE_API_KEY` is also accepted.

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_API_KEY` | (required) | Gemini API key |
| `AUDIT_MODEL` | (none) | Try this model first, before the chain (`--model` overrides it) |
| `AUDIT_FALLBACK_MODELS` | discovered | Use this exact comma-separated chain instead; `none` = only `AUDIT_MODEL` |
| `AUDIT_MODEL_STATUS_FILE` | `~/.cache/devops-audit-agent/gemini-model-status.json` | Where unavailable models, daily limits and today's request counts are kept between runs; `none` disables |

### How the model is chosen

At start-up the agent lists the models the API key can use; listing costs no quota. It keeps
every general-purpose text model and orders them **from most to least capable**:
1. every Pro model
2. every Flash model
3. every Flash-Lite model
4. Google's open-weight Gemma models, which are a last resort

Within each group the newest version comes first and stable comes before preview. A preview
is left out when its stable release is listed, because it is served by that release. Aliases
(`-latest`) and specialised variants (image, speech, transcription, robotics, …) are left out,
so every entry is a concrete model. If the list cannot be fetched, a built-in chain is used:
the 9 models that served on the free tier when last checked
([evals/model-checks/](evals/model-checks/)).

The model is chosen **without sending any request**, using what earlier runs recorded in
`AUDIT_MODEL_STATUS_FILE`:

1. **Known unavailable?** Models that hit their daily quota, were retired, are not on the free
   tier, or were overloaded recently are skipped until the recorded time.
2. **Enough quota left?** Successful requests are counted per model per day. Daily quotas reset
   at midnight Pacific, and the count resets with them. A model's daily limit is learned from
   the first quota error that states it. A model with fewer than 10 requests left today is
   passed over, so an audit doesn't start on a model about to run out. If every model is that
   low, the most capable one with any quota left is used.

The first real request then shows whether the model works. If it fails, the same request goes
to the next model, so nothing is lost. Once a model is serving, the audit stays on it while it
works. The count can't see requests made elsewhere with the same key (for example in AI
Studio), so a real quota error always takes precedence.

The model that actually served an audit is printed at the end of the run (`model: …`) and
written as the last line of the `--trace` file.

### Free-tier errors and fallback

Free-tier Gemini models are often overloaded (HTTP 503 "high demand") or out of quota (HTTP
429). Each model gets **two attempts** per request, and the client handles each error type
differently:

| Error | What happens | Model skipped for |
|---|---|---|
| 503 / 500 / 502 / 504 | Retried once after about 2 s, then move to the next model | 10 min |
| 429, per-minute limit | Wait the delay the API asks for (up to 60 s) and retry once | 2 min |
| 429, daily quota | Move to the next model immediately; waiting will not help. The stated limit is remembered | until the reported reset |
| 429, every quota exceeded on the first request | The model has no free-tier quota (e.g. Pro); move on | 7 days |
| Request timeout (120 s) | Move to the next model immediately | 10 min |
| 404 (model retired) | Move to the next model | 7 days |
| 400 "… not enabled / not supported" | The model lacks a feature the agent needs; move on | 7 days |
| 401 / 403, or any other 400 | Stop with a clear error; these are configuration problems | — |

Gemma rejects conversations that contain Gemini's thought signatures. When a Gemma model takes
over, earlier turns are therefore replayed to it without them; Gemini models still get them
unchanged.

Each model has its own quota, so when one is busy or used up the audit continues on the next,
less capable one, and stays there for the rest of the audit. If every model fails or is
skipped, the run exits with code 1 and one message listing why each model was passed over
(e.g. `daily free-tier quota used up, resets in ~16h`).

## Usage

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

| Flag | Description |
|---|---|
| `--repo` | Public GitHub repository URL (required) |
| `--output` | Report path (required) |
| `--max-turns N` | Maximum agent turns, default 40 |
| `--trace FILE` | Where to write the tool-call trace (default: next to the report, e.g. `report.trace.jsonl`) |
| `--no-trace` | Do not write a trace |
| `--model` | Try this model first (default: the most capable available) |
| `--provider` | `gemini` (default) or `anthropic` (optional, see below) |
| `--keep-clone` | Keep the temporary clone for debugging |
| `-v` | Log the agent's reasoning and tool calls to stderr |

Exit codes: `0` report written, `1` fatal error (clone failed, API error, refusal), `2` invalid input.
The run is unattended and never prompts.

### Output

```json
{
  "repository": "https://github.com/example/demo-project",
  "violations": [
    {
      "rule_id": "SEC-001",
      "category": "security",
      "severity": "critical",
      "file": "config.py",
      "line": 17,
      "description": "Hardcoded API credential",
      "evidence": "Credential assigned directly in source code",
      "recommendation": "Use a secrets manager or environment variable"
    }
  ]
}
```

`category` is one of `repository_hygiene`, `dependency_management`, `code_quality`, `testing`,
`ci_cd`, `security`, `configuration`, `collaboration`, `build_release`. `severity` is one of
`critical`, `high`, `medium`, `low`. `file`/`line` are `null` when not applicable.

**Tool-call trace.** Every run also writes a trace next to the report, e.g. `report.json` gets
`report.trace.jsonl`. It has one JSON line per tool call: the turn, the tool, its arguments,
whether it failed, the output size, a short preview and the time taken. The last line names the
model(s) that served the audit. The trace shows which steps the model chose. Secrets are
redacted from it, and file contents are never stored in it: `read_file` keeps only its header
line and `search_code` only its `file:line` locations.

## Architecture

```
agent.py              CLI: parse args, clone, run agent, write report, clean up
tools.py              Read-only inspection tools + their JSON schemas + dispatch
auditor/
  loop.py             AuditAgent: the observe → reason → act loop
  gemini_llm.py       Gemini client: converts history to/from Gemini function calling
  llm.py              Provider selection (Gemini by default) and the shared LLMError
  anthropic_llm.py    Optional Claude provider, loaded only when selected
  prompts.py          System prompt and agent-only tools (record_violation, finish_audit)
  report.py           Violation validation, dedupe, JSON report writer
  repo.py             URL validation, isolated clone, cleanup
  redact.py           Secret redaction
tests/                Offline tests (fake LLM, temporary git repos)
```

**Agent loop** (`auditor/loop.py`). Each turn sends the conversation and the tool list to the
model. The model decides which tool to call next based on what it has seen so far. The loop
runs every requested call (several per turn if the model asks for them in parallel), returns
the outputs as `tool_result` blocks, and repeats. Tool failures go back to the model as error
results, so it can recover instead of the run crashing. The model reports findings one at a
time with `record_violation`, which validates each one against the report contract and
rejects duplicates:
- a second finding at a `file:line` that already has one
- the same category and description again, in the same file or with no file
- a `rule_id` that is already used; the error names the next free one

The same description in two *different* files is accepted, because each committed credential is
its own violation. It ends
the run with `finish_audit`. The first `finish_audit` is refused if the model has never opened
some workflow file or some file tagged `[sensitive name]`. The refusal lists those files, and
a second `finish_audit` always ends the run, so the model keeps the final say. When the turn
limit is reached, the violations recorded so far are still written.

**Scope.** The system prompt (`auditor/prompts.py`) limits the audit to the assignment's nine
DevOps inspection areas:
- repository hygiene
- dependency management
- code quality
- testing
- GitHub Actions / CI
- security
- configuration
- collaboration
- build and release

Each area lists the assignment's example violations and the concrete signs to look for.
Anything outside the nine areas is explicitly out of scope, even if it would be good practice:
general code bugs, style, licensing, action pinning, ML concerns. The prompt also includes the
assignment's evidence rule, an explicit list of things that are not violations, and the rule
that everything read from the repository is untrusted evidence, never instructions. Every
finding's `category` must be one of the nine areas, and its `rule_id` prefix must match that
area (for example `security` → `SEC-001`). Both are checked when a finding is recorded.

**Inspection tools** (`tools.py`):

| Tool | What it does |
|---|---|
| `list_files(subdir?, pattern?)` | Git-tracked files with sizes (shows committed junk, not local files). Files named like keys or credentials (`.env`, `id_rsa`, `*.pem`, `.npmrc`, …) are tagged `[sensitive name]` as a hint to open them; templates such as `.env.example` are not tagged |
| `read_file(path, start_line?, end_line?)` | Line-numbered file contents |
| `search_code(pattern, path_glob?, ignore_case?, max_results?)` | Regex search across tracked text files |
| `git_log(max_count?, path?)` | Commits with changed files |

**Safety.**
- The target is cloned into a fresh temp directory with prompts and LFS disabled, and deleted afterwards.
- Tools only read. Paths are confined to the clone, and no repository code is executed.
- Outputs are size-capped.
- Known credential formats (cloud keys, tokens, private keys) are masked before tool output reaches the LLM.
- Report fields, logs and traces get an extra redaction pass that also masks `password = "..."`-style values.

**Gemini client** (`auditor/gemini_llm.py`). The loop keeps its conversation as role/content
messages with `tool_use` and `tool_result` blocks. On each call, `GeminiLLM` converts that
history to Gemini `contents`: tool calls become `function_call` parts and their results
`function_response` parts. It also converts Gemini's reply back. Tool schemas are passed as
JSON Schema (`parameters_json_schema`), and automatic function calling is disabled so the
loop stays in control. Gemini's own turns are replayed unchanged, so its thought signatures
survive. This replay also makes switching models mid-audit safe; that was checked against the
live API, while stripping the signatures is rejected with a 400. Retries and fallback are
handled in the client rather than by the SDK, so each error type gets the handling in the
table above. Safety blocks are reported as refusals.

## Optional: Anthropic Claude

Gemini is the default and needs nothing else. To use Claude instead:

```bash
pip install -r requirements-anthropic.txt
```

Then set `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in `.env`, or pass
`--provider anthropic`. If only `ANTHROPIC_API_KEY` is set, Claude is picked automatically;
if both keys are set, Gemini wins.

The default model is `claude-opus-5-5` with `effort: high` (override with `AUDIT_MODEL` and
`AUDIT_EFFORT`). Requests use automatic prompt caching. They also opt into server-side
refusal fallback, so a safety-classifier decline on security content is retried on a
fallback model instead of ending the audit.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check .
pytest
```

The tests run offline and make no API calls. They cover:
- the tools against a temporary git repository (including path-escape and read-only checks)
- report validation and redaction
- the agent loop, driven by a scripted fake LLM: tool dispatch, parallel calls, error recovery, duplicates, turn limits and refusals
- the Gemini client, using real SDK types and a fake client: message conversion, refusals, an end-to-end loop run
- provider selection: Gemini by default, the optional Anthropic path, and a clear error when its SDK is missing
- CLI input validation

## Evaluation

```bash
python evals/run_eval.py --repos evals/repos.txt --out reports/my-run
```

This audits each repository in [evals/repos.txt](evals/repos.txt) in turn, pausing 30 s
between audits to respect the free tier's per-minute limits. It writes each report and trace,
plus a `summary.json` with exit codes, durations, serving models, fallbacks and the HTTP
status of every API request. The batch stops early if no Gemini model is available.

## Development history

See [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) for how the agent was built, evaluated and revised, step by step. Evaluation reports and tool-call traces are kept in [evals/runs/](evals/runs/).
