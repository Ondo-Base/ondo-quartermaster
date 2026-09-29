# CI/CD

What runs, when, and what it protects. Everything here lives in `.github/`.

## On every pull request and every push to `main`

| Workflow | Job (status check name) | Fails when |
| --- | --- | --- |
| `ci` | **Lint and typecheck** | `ruff check` or `ruff format --check` fails on the agent; `actionlint` finds a broken workflow; TypeScript does not typecheck |
| `ci` | **Agent tests** | Any agent test fails: both model wire formats, files, decision layer, real Chromium (Stage 3), real GTK app on a virtual display (Stage 4), the control-plane integration. Also fails if `agent/config/thresholds.json` no longer matches what calibration measures |
| `ci` | **Control plane and web tests** | Server or web tests fail, or either does not build |
| `ci` | **Container image builds** | The Dockerfile does not build, or the image does not come up healthy and non-root |
| `security` | **Secret scan** | gitleaks finds a secret anywhere in the history the PR adds |
| `security` | **Dependency review** (off by default, see below) | The PR adds a dependency with a high or critical advisory, or a GPL-3.0/AGPL-3.0 licence |
| `security` | **Dependency audit** | A shipped npm dependency (server, web) or a declared agent dependency has a known high or critical advisory |
| `codeql` | **CodeQL (python)**, **CodeQL (javascript-typescript)** | Reports security findings to Security → Code scanning |

`security` and `codeql` also run weekly, so a new advisory against code that has
already merged still shows up.

Lint runs first and the three test jobs wait for it, so a formatting slip fails
in about a minute instead of after the full suite.

## Releases (continuous delivery)

Push a version tag that matches `agent/pyproject.toml`:

```sh
git tag v0.1.0 && git push origin v0.1.0
```

`release.yml` then:

1. runs the whole `ci` workflow again, and stops if anything fails;
2. refuses a tag that does not match the agent's version;
3. builds the control-plane image (server + web UI) and pushes it to
   `ghcr.io/dandan002/ondo-quartermaster/control-plane` with semver and SHA tags,
   an SBOM, and signed build provenance;
4. builds the agent wheel and sdist plus a config bundle, attests them, and
   publishes a GitHub release with `SHA256SUMS`.

It deploys nothing. Where the image runs, and with which secrets, is the
operator's decision. Verify an artefact before using it:

```sh
gh attestation verify oci://ghcr.io/dandan002/ondo-quartermaster/control-plane:0.1.0 -R dandan002/ondo-quartermaster
gh attestation verify ondo_agent-0.1.0-py3-none-any.whl -R dandan002/ondo-quartermaster
```

## Windows, by hand

`windows.yml` runs only when someone starts it (Actions, **windows**, **Run
workflow**). It is not a required check, because the Windows side has not been
proven yet, and this is how to prove it. It does three things:

- runs the agent tests on a GitHub-hosted Windows runner;
- builds the Intune package with `deploy/windows/build.ps1`;
- installs, detects and uninstalls that package, then uploads it as an
  artifact.

See `docs/deploy-windows.md`.

## Dependencies

Dependabot opens grouped weekly PRs for npm, the agent's Python dependencies,
GitHub Actions and the Docker base image. They go through the same checks as
any other change.

## Before you commit

```sh
pip install pre-commit
pre-commit install                       # on commit: ruff, gitleaks, actionlint, file hygiene
pre-commit install --hook-type pre-push  # on push: TypeScript typecheck
```

The hooks are the same checks CI runs, so skipping them only moves the failure
later.

## Repository settings to switch on (one time, by an admin)

Workflows can require checks, but only repository settings can *enforce* them.
In **Settings**:

1. **Rules → Rulesets → New branch ruleset**, target `main`, enforcement *Active*:
   - Restrict deletions; block force pushes.
   - Require a pull request before merging, with **0 required approvals** and
     *Require review from Code Owners* **off** (with one maintainer, either
     would block every merge). Keep *Require conversation resolution* on.
     Raise approvals to 1 and turn Code Owner review on when a second
     maintainer joins.
   - Require status checks to pass, with *Require branches to be up to date*:
     `Lint and typecheck`, `Agent tests`, `Control plane and web tests`,
     `Container image builds`, `Secret scan`, `Dependency audit`,
     `CodeQL (python)`, `CodeQL (javascript-typescript)`.
     (A check can be selected once it has run at least once.)
   - Require code scanning results: CodeQL, blocking on *High or higher*.
   - Optionally, require signed commits.
2. **Rules → Rulesets → New tag ruleset**, target `v*`: restrict creations,
   updates and deletions to admins, so only a maintainer can cut a release.
3. **Actions → General → Workflow permissions**: *Read repository contents*
   (workflows ask for more only where they need it), and leave *Allow GitHub
   Actions to create and approve pull requests* off.
4. **Code security**: turn on Dependency graph, Dependabot alerts, Dependabot
   security updates, Secret scanning and **Push protection** (which blocks a
   secret before it reaches GitHub at all).
5. **Dependency review** is skipped until the Dependency graph is on. To turn
   it on: enable Dependency graph (step 4), then add the repository variable
   `DEPENDENCY_REVIEW` = `true` (**Secrets and variables → Actions →
   Variables**), and add `Dependency review` to the required checks.

Until then the gate is the required checks, not a second person: nothing
merges to `main` without a pull request that passes all of them.
