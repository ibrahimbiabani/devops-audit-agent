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
| `AUDIT_MODEL` | `gemini-flash-latest` | Gemini model to use |

**Free tier.** Per-minute rate limits (HTTP 429) and temporary overload (HTTP 503) are
retried automatically with exponential backoff. A full audit makes up to `--max-turns`
requests, so the daily quota limits how many audits you can run per day. If
`gemini-flash-latest` is congested, `AUDIT_MODEL=gemini-flash-lite-latest` responds much
faster, at some cost in reasoning quality.

## Usage

```bash
python agent.py --repo https://github.com/<owner>/<repo> --output report.json
```

| Flag | Description |
|---|---|
| `--repo` | Public GitHub repository URL (required) |
| `--output` | Report path (required) |
| `--max-turns N` | Maximum agent turns, default 40 |
| `--trace trace.jsonl` | Write one JSON line per tool call (redacted) |
| `--model` | Override the model (e.g. `gemini-flash-lite-latest`) |
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
rejects duplicates, including a second finding at a `file:line` that already has one. It ends
the run with `finish_audit`. The first `finish_audit` is refused if the model has never opened
some workflow file or some file tagged `[sensitive name]`. The refusal lists those files, and
a second `finish_audit` always ends the run, so the model keeps the final say. When the turn
limit is reached, the violations recorded so far are still written.

The system prompt (`auditor/prompts.py`) treats everything read from the repository as
untrusted evidence, never as instructions. It also gives a general checklist of DevOps and
GitHub Actions anti-patterns, and an explicit list of things that are not violations, to keep
false positives down.

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
survive. Safety blocks are reported as refusals. Auth, quota, model and network failures
become an `LLMError` with a clear message.

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

## Development history

See [docs/DEVELOPMENT_LOG.md](docs/DEVELOPMENT_LOG.md) for how the agent was built, evaluated and revised, step by step. Evaluation reports and tool-call traces are kept in [evals/runs/](evals/runs/).
