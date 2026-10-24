"""inspect_workflows: facts with correct line numbers, never secret values, never verdicts."""

import pytest

from auditor.workflows import summarize
from tools import RepoTools, ToolError

RELEASE = """\
name: Release
on:
  push:
    branches: [main]
jobs:
  publish:
    runs-on: ubuntu-latest
    permissions:
      id-token: write
    steps:
      - uses: actions/checkout@v4
      - name: Install
        run: python -m pip install build nox
      - name: Publish
        uses: pypa/gh-action-pypi-publish@release/v1
"""

RISKY = """\
on:
  pull_request_target:
  issues:
    types: [opened]
permissions: write-all
jobs:
  triage:
    if: ${{ contains('alice,bob', github.actor) }}
    runs-on: ${{ github.event.issue.title }}
    env:
      API_KEY: "sk-not-a-real-value-123"
    steps:
      - uses: actions/checkout@v4
        with:
          ref: ${{ github.event.pull_request.head.ref }}
      - run: |
          set -x
          echo "Title: ${{ github.event.issue.title }}"
          echo "token is $DEPLOY_TOKEN"
          curl -v -H "Authorization: Bearer ${{ secrets.DEPLOY_TOKEN }}" https://example.com
          curl -fsSL https://example.com/install.sh | bash
      - run: npm test || true
"""


def test_release_facts_show_an_ungated_publish():
    out = summarize(".github/workflows/release.yml", RELEASE)
    assert "on: push (branches: main)" in out
    assert "job publish (line 6)" in out  # the job's own line, not its first setting
    assert "needs: none" in out and "permissions (job level): id-token: write" in out
    assert "steps that publish, deploy or push: line 14 (Publish)" in out
    assert "steps that run tests: none" in out  # `pip install ... nox` installs, it does not test


def test_risky_workflow_signals_with_lines():
    out = summarize("w.yml", RISKY)
    assert "triggers that carry outside input or run with repository privileges: pull_request_target, issues" in out
    assert "permissions (workflow level): write-all" in out
    for expected in (
        "line 8 [triage] actor/name check with contains()",
        "line 9 [triage] untrusted input in runs-on: ${{ github.event.issue.title }}",
        "line 11 [triage] literal value assigned to API_KEY (value not shown)",
        "line 15 [triage] checkout of a pull request ref",
        "line 17 [triage] bash debug mode (set -x)",
        "line 18 [triage] untrusted input in run/with/env: ${{ github.event.issue.title }}",
        "line 19 [triage] secret-like value printed",
        "line 20 [triage] verbose HTTP client",
        "line 20 [triage] secret used: secrets.DEPLOY_TOKEN",
        "line 21 [triage] remote script piped to a shell",
        "line 22 [triage] step failure ignored",
    ):
        assert expected in out, expected
    assert "sk-not-a-real-value-123" not in out  # literal values are described, never shown


def test_reports_facts_not_verdicts():
    out = summarize("w.yml", RISKY).lower()
    assert "violation" not in out and "vulnerab" not in out and "should" not in out


def test_unparseable_and_non_mapping_files():
    assert "could not be parsed as YAML" in summarize("bad.yml", "on: [push\njobs:")
    assert "not a workflow" in summarize("list.yml", "- a\n- b\n")


@pytest.fixture
def workflow_repo(tmp_path):
    wf = tmp_path / ".github" / "workflows"
    (wf / "controls").mkdir(parents=True)
    (wf / "release.yml").write_text(RELEASE)
    (wf / "controls" / "fixture.yml").write_text(RISKY)  # GitHub never runs nested workflows
    return RepoTools(tmp_path)


def test_tool_lists_runnable_workflows_and_names_ignored_ones(workflow_repo):
    out = workflow_repo.inspect_workflows()
    assert out.startswith(".github/workflows/release.yml")
    assert "Not run by GitHub" in out and "controls/fixture.yml" in out
    assert "line 9 [triage]" not in out  # the nested file is not inspected


def test_tool_single_path_and_bad_path(workflow_repo):
    assert "job publish" in workflow_repo.inspect_workflows(".github/workflows/release.yml")
    with pytest.raises(ToolError, match="Not a workflow file GitHub runs"):
        workflow_repo.inspect_workflows(".github/workflows/controls/fixture.yml")


@pytest.mark.parametrize(
    "command", ["          env", "      - run: env", "          printenv | grep -i token", "          env | sort"]
)
def test_environment_dump_commands_are_signalled(command):
    # Regression (2026-10-24, ngocdh/insecure-devops): a bare `env` line was not reported.
    assert "environment dump" in summarize("w.yml", f"on: push\njobs:\n  j:\n    steps:\n      - run: |\n{command}\n")


@pytest.mark.parametrize(
    "line", ["    env:", "      NODE_ENV: production", "    environment: prod", "      - run: echo $ENV_FILE"]
)
def test_env_keys_and_names_are_not_environment_dumps(line):
    assert "environment dump" not in summarize("w.yml", f"on: push\njobs:\n  j:\n{line}\n")
