# Answer keys for the evaluation repositories

Verified on 2026-10-24 by reading every workflow and key file in each repository (read-only,
through the GitHub API). None of the repositories changed after the first evaluation round
(2026-10-21): Grafter's last push was 2026-09-29, and the others are older.

An item is **in scope** if it falls in one of the assignment's nine inspection areas *and* the
agent's rules treat it as a violation. Items the rules deliberately do not report are listed
as **excluded by design**, with the reason, so their cost to recall is visible.

## A — Plazmaz/leaky-repo (obvious defects)

- **Source:** `.leaky-meta/secrets.csv`, the repository's own key.
- **Key:** **42 files containing at least one real ("risk") secret**, 96 secret lines in total.
  Two more files hold only "informative" data (`.ssh/id_rsa.pub`, `high-entropy-misc.txt`)
  and are not violations.
- **Correction:** earlier steps of `docs/DEVELOPMENT_LOG.md` scored against "35 files". That
  number did not come from the repository and was wrong. Corrected scores: Step 5 14/42,
  Step 11 9/42, Step 13 16/42, Step 15 15/42.
- **Never found in any round (26 files):** mostly generic names whose secrets are only visible
  in the content, e.g. `wp-config.php`, `settings.py`, `db/dump.sql`, `.bash_profile`,
  `filezilla/*.xml`, `.mozilla/firefox/logins.json`, `deployment-config.json`.

## B — paastha1/cicd-test-repo

- **Source:** README, 6 patterns; `clean-workflow.yml` is the clean control.
- **Each pattern appears in two workflow files:**

| # | Pattern | Instances |
|---|---|---|
| 1 | Hardcoded secrets in `env:` | `deploy.yml` 11–13 (AWS key id, AWS secret, DB password); `release.yml` 11–13 (PyPI, Docker Hub, GitHub tokens) |
| 2 | Secrets echoed / `printenv` | `ci.yml` 25–27; `release.yml` 25–26 |
| 3 | `set -x` near secrets | `deploy.yml` 21; `release.yml` 20 (`set -ex`) |
| 4 | Verbose curl with auth header | `deploy.yml` 27; `release.yml` 27 |
| 5 | Secrets as CLI arguments | `ci.yml` 31–32 (`npm publish --token`, `docker login -p`); `release.yml` 31 (`twine --password`) |
| 6 | Sensitive files as artifacts | `ci.yml` 38–41; `release.yml` 37–40 |

- **Real issues beyond the README:**
  - `ci.yml` publishes a package from a `pull_request`-triggered job;
  - `deploy.yml` deploys on every push to `main` with no test step;
  - `release.yml` publishes with no test step.
- **`deploy.yml` 14:** `SLACK_WEBHOOK` is an obvious placeholder (`…/FAKE/WEBHOOK/NOTREAL`). A
  finding on it is borderline.

## B — Grafter-labs/grafter-lab-injection

- **Source:** README, 6 scenarios; `controls/` is a nested folder GitHub never runs.

| # | Scenario | Status |
|---|---|---|
| 1 | `script-injection.yml` 15: `issues` event title in `run:` | in scope |
| 2 | `runner-control-injection.yml` 13: issue title chooses `runs-on:` | in scope |
| 3 | `bypassable-actor-allowlist-gate.yml` 12: `contains('alice,bob,octocat', github.actor)` gate | in scope (ineffective gate) |
| 4 | `filename-collision-download.yml` 17: PR `head.ref` in a download URL, two downloads to one name | in scope (untrusted input in `run:`) |
| 5 | `secret-echoed-to-log.yml` 16: secret-shaped `DEMO_SECRET` echoed | in scope (README: the literal is not a real secret) |
| 6 | `deprecated-commands.yml` | **excluded by design:** deprecated syntax is out of scope (Step 9) |

- **Real issues beyond the README:**
  - `test.yaml` 45–47: `curl -v` with an `Authorization` header built from secrets;
  - `contents: write` on the `issue_comment` and `issues` workflows.
- **`test.yaml` 10–11:** `OSSRH_USERNAME`/`OSSRH_PASSWORD` are literal `dummy-…` placeholders.
  A finding on them is borderline.

## B — gary-mello/github-vuln-actions (held out)

- **Source:** README, 8 rows.

| # | README row | Status |
|---|---|---|
| 1 | `issue-labeler.yml` 18, 22: issue title/body in `run:` | in scope |
| 2 | `pr-comment-handler.yml` 16–17: comment body in `run:` | in scope |
| 3 | `pr-title-injection.yml` 17–19: PR title/body/head.ref in `run:` | in scope |
| 4 | `prt-dangerous-checkout.yml` 19: `pull_request_target` + checkout of the PR head, then `npm test` | in scope |
| 5 | `write-all-permissions.yml` 12: `permissions: write-all` on `pull_request_target` | in scope |
| 6 | `no-permissions-declared.yml`: no `permissions:` on a push/PR build workflow | **excluded by design** (Step 14: only reported where the default token is risky) |
| 7 | `unpinned-actions.yml`: `@main`, `@latest` and tags instead of SHAs | **excluded by design** (Step 9: action pinning is out of scope). `@main`/`@latest` may count as "weak version constraints", see the notes below. |
| 8 | `self-hosted-runner.yml`: `runs-on: [self-hosted, …]` | **not a violation:** the README gives it no severity ("—"), and the trigger is push to `main` only, so outsiders cannot reach the runner |

- **Real issues beyond the README:**
  - `write-all-permissions.yml` 21: `gh pr merge --auto` on `pull_request_target`, with no review;
  - `self-hosted-runner.yml` 24 and `unpinned-actions.yml`: deploy/push to a registry on every
    push to `main` with no tests.
- **Fixture artefacts (defensible but not counted):** `npm ci` with no `package.json` or
  lockfile, no `.gitignore`, no linter.

## B — jpr5/vulnerable-workflows-test (held out)

- **Source:** no list in the repository. This key was built by reading the 5 workflows and
  `package.json`.

| # | Item | Status |
|---|---|---|
| 1 | `auto-label.yml` 12: `pull_request_target` + checkout of `head.ref` | in scope |
| 2 | `greet-pr.yml` 12–13: PR title/user login in `run:` | in scope |
| 3 | `manual-deploy.yml` 19: `inputs.version`/`inputs.environment` in `run:` | in scope (only users with write access can trigger it, so lower severity) |
| 4 | `setup.yml` 10: `curl … \| bash` | in scope (unverified remote code in CI) |
| 5 | `setup.yml` 15: hardcoded `API_KEY` | in scope |
| 6 | Missing `permissions:` on the `pull_request_target` workflow | in scope |
| 7 | `ci.yml` 2: `on: push` only, so pull requests are never validated | in scope (incorrect triggers); **added in this review** |
| 8 | `package.json`: `"test": "echo ok"`, no real tests | in scope (missing tests); **added in this review** |

## C — pypa/sampleproject (mostly healthy)

| # | Item | Status |
|---|---|---|
| 1 | `release.yml` 6–9, 28: publishes to PyPI on every push to `main`, no test step, no `needs:` | in scope (medium; `skip-existing: true` limits it to new versions) |

- **Not violations:** the job-level `id-token: write` (least privilege), flake8 run via
  `nox -s lint` in CI, the missing `permissions:` on the build/test workflow, and the
  unconstrained `peppercorn` dependency (a library; PyPA guidance is loose constraints).

## C — pallets/itsdangerous (mostly healthy, held out)

- **Key: no violations.** Actions are pinned to SHAs, there is a `uv.lock` lockfile, tests and
  typing run via tox in CI, linting runs via pre-commit, the `.gitignore` is suitable, and
  permissions are declared where tokens write (`lock.yaml`, `publish.yaml` jobs).
- **Nuance:** `publish.yaml` publishes on tag push without running tests in that workflow. It
  is a deliberate maintainer release, the build is a `needs:` dependency, and publishing goes
  through a protected `environment: publish`. That is not a violation, but the Build and
  release rule could be misread to flag it.

## Notes for the rules (from this review)

1. **The planted-defect keys are per file.** gary-mello lists the same expression injection as
   three README rows, one per workflow, and paastha1's patterns each appear in two workflows.
   The agent's granularity rule ("the same anti-pattern in several places is one violation")
   merges them into one finding, which a per-file key scores as one hit.
2. **`@main`/`@latest` action references** are arguably "weak version constraints" (Dependency
   management). Version tags such as `@v4` are not.
3. **Tag- or release-triggered publishing through a protected `environment:`** (itsdangerous)
   should not be confused with publishing on every branch push (sampleproject).
