# DevOps Repository Auditing Agent

An LLM agent that clones a public GitHub repository, investigates it with read-only inspection
tools, and writes a JSON report of DevOps best-practice violations with evidence and remediation.

The agent loop, tool dispatch and control flow are hand-written Python. It works with either
Anthropic (Claude) or Google (Gemini), using only their official SDKs (`anthropic`,
`google-genai`). No agent frameworks are used.

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

Put **one** API key in `.env` (or export it in the shell):

| Provider | Variable | Default model |
|---|---|---|
| Anthropic | `ANTHROPIC_API_KEY` | `claude-opus-5-5` |
| Google Gemini | `GEMINI_API_KEY` | `gemini-flash-latest` |

The provider is chosen from whichever key is set (Anthropic if both are). Optional variables:

| Variable | Purpose |
|---|---|
| `LLM_PROVIDER` | Force `anthropic` or `gemini` |
| `AUDIT_MODEL` | Override the model for the chosen provider |
| `AUDIT_EFFORT` | Anthropic reasoning effort (`low` … `max`, default `high`) |

On the Gemini free tier, per-minute rate limits are absorbed by automatic retries with backoff.
A full audit takes up to `--max-turns` requests, so the daily request quota limits how many
audits you can run per day.

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
| `--provider` | `anthropic` or `gemini` (overrides `LLM_PROVIDER`) |
| `--model` | Override the model |
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
  llm.py              Provider selection; Anthropic Messages API wrapper and error mapping
  gemini_llm.py       Gemini adapter: converts history to/from Gemini function calling
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
rejects duplicates. It ends the run with `finish_audit`. When the turn limit is reached, the
violations recorded so far are still written.

**Inspection tools** (`tools.py`):

| Tool | What it does |
|---|---|
| `list_files(subdir?, pattern?)` | Git-tracked files with sizes (shows committed junk, not local files) |
| `read_file(path, start_line?, end_line?)` | Line-numbered file contents |
| `search_code(pattern, path_glob?, ignore_case?, max_results?)` | Regex search across tracked text files |
| `git_log(max_count?, path?)` | Commits with changed files |

**Safety.**
- The target is cloned into a fresh temp directory with prompts and LFS disabled, and deleted afterwards.
- Tools only read. Paths are confined to the clone, and no repository code is executed.
- Outputs are size-capped.
- Known credential formats (cloud keys, tokens, private keys) are masked before tool output reaches the LLM.
- Report fields, logs and traces get an extra redaction pass that also masks `password = "..."`-style values.

**Providers.** The loop keeps its conversation in one format. `ClaudeLLM` sends it to the
Messages API as-is. `GeminiLLM` converts it to Gemini `contents` on each call (tool calls
become `function_call`/`function_response` parts) and converts replies back. Gemini's own turns
are replayed unchanged so its thought signatures survive. Both raise `LLMError` with a clear
message on auth, quota, model or network failures.

**Anthropic settings.** The default model is `claude-opus-5-5` with `effort: high`. Requests use
automatic prompt caching, so the growing conversation is not re-billed in full each turn.
Requests also opt into server-side refusal fallback (`fallbacks: "default"`), so a
safety-classifier decline on security-related content is retried on a fallback model instead
of aborting the audit.

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
- the Gemini adapter, using real SDK types and a fake client: message conversion, refusals, an end-to-end loop run, provider selection
- CLI input validation
