<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-ea8c: Automated PR review and a safer unattended merge flow](#adr-ea8c-automated-pr-review-and-a-safer-unattended-merge-flow)
  - [Context](#context)
  - [Options](#options)
  - [Decision](#decision)
    - [1. Identity: a second GitHub App, `chb-factory-reviewer`](#1-identity-a-second-github-app-chb-factory-reviewer)
    - [2. Where it runs: a `review` workflow on `workflow_run`](#2-where-it-runs-a-review-workflow-on-workflow_run)
    - [3. Typed result and the gate](#3-typed-result-and-the-gate)
    - [4. Risk classes and who approves](#4-risk-classes-and-who-approves)
    - [5. Bounded loop](#5-bounded-loop)
    - [6. Evidence and validation](#6-evidence-and-validation)
    - [7. Model and cost](#7-model-and-cost)
    - [8. Staged rollout](#8-staged-rollout)
  - [Alternatives rejected](#alternatives-rejected)
  - [Owner decisions](#owner-decisions)
  - [Consequences](#consequences)
  - [Unverified](#unverified)
  - [Sources](#sources)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-ea8c: Automated PR review and a safer unattended merge flow

- Status: accepted (2026-11-06)
- Date: 2026-11-06
- Ticket: CHB-82
- Amended by CHB-93 (decision 2) and CHB-105 (decisions 2, 3 and 7, owner decision 4)

## Context

Today the owner approves every agent PR.
The 540 talk replaced diff reading with a scored review: five weighted dimensions, Merge Safety of at least 8.5, no critical finding, at most 3 rounds.
eferro's rule applies to the reviewer too: a gate reads a typed result or an exit code, never prose.

Facts about this repository on 2026-11-06:

- The agent acts as the GitHub App `chb-factory-agent` (ADR-d7b7). The App has `workflows: write`.
- The `main` ruleset requires 1 approval, code-owner review, approval of the last push, and the checks `backend`, `frontend` and `e2e` from GitHub Actions (app 15368).
- The repository has no CODEOWNERS file. `require_code_owner_review` has no effect until CHB-29 lands.
- The repository is public and owned by a user account, so it has no teams.
- Copilot code review already comments on PRs (#5, #6, #8). It leaves `COMMENTED` reviews only.
- Dependabot auto-merge (ADR-ebe6) approves with an App token. No merged PR yet shows that an App approval counts toward the required approval.

What GitHub documents:

- Approving reviews count from "people with write permissions in the repository or from a designated code owner". The docs do not say whether a GitHub App approval counts.
- Code owners are "individuals or teams" with write permission. Apps are not listed. Without teams, an App cannot satisfy code-owner review.
- A required check can name an app as its source. "If the status is set by any other person or integration, merging won't be allowed."
- A skipped job reports success and "will not prevent a pull request from merging, even if it is a required check". `neutral` is also treated as success.
- "Any user with write access to your repository has read access to all secrets configured in your repository."
- On `pull_request`, the workflow file comes from the PR merge commit, and same-repository branches receive secrets. Forks receive none.
- On `pull_request_target`, the workflow file and `GITHUB_SHA` come from the default branch, and environment rules evaluate against it (since 2025-12-08).
- From 2026-12-02 GitHub blocks `pull_request_target` on public repositories unless an Actions event policy allows it. This personal repository has no event policies (checked 2026-11-06, CHB-93).
- On `workflow_run`, the workflow file comes from the default branch, and `GITHUB_SHA` and `GITHUB_REF` are the default branch's. The triggered workflow gets secrets even if the triggering one did not.
- An environment's deployment branch rule "is matched against the `GITHUB_REF` of the workflow run". A `workflow_run` job therefore matches `main`.

## Options

| Option | Typed gate | Separate identity | Cost and secret | Verdict |
|---|---|---|---|---|
| Claude Code CLI in a GitHub Actions job, a review skill from `main`, a reviewer App | Yes: `--json-schema` | Yes | Subscription token since CHB-105; Claude credential and App key in GitHub | ⭐ |
| `anthropics/claude-code-action` running the same skill | Yes: `structured_output` | Yes, with `github_token` from a custom App | Same | ❌ more surface than needed |
| Claude Code in the local orchestrator, posting as a reviewer App | Yes | Weak: same machine and 1Password as the author | Subscription | ❌ for the gate |
| Claude Code Review (managed) | No: never approves or blocks; its check "doesn't conclude as a failure" | Claude GitHub App | Team or Enterprise plan | ❌ |
| GitHub Copilot code review | No rubric or score; not a required check | Copilot | AI credits | Keep as second opinion |

## Decision

### 1. Identity: a second GitHub App, `chb-factory-reviewer`

- Installed on this repository only.
- Permissions: contents read, pull requests write, checks write, metadata read. No contents write, so it cannot merge (merge needs contents write). No workflows, no administration.
- The author App never holds a reviewer credential. The reviewer App never pushes.
- The author App loses `workflows: write`. Without it, an agent branch cannot change a workflow. Workflow changes go through the owner. This amends ADR-d7b7.

### 2. Where it runs: a `review` workflow on `workflow_run`

Amended by CHB-93. The second version ran on `pull_request_target`, which GitHub blocks on public repositories from 2026-12-02.
The first version (CHB-87) used `pull_request`, where a branch that edits `review.yml` runs its own copy with the reviewer secrets.

- Two workflows:
  - `review-trigger.yml` runs on `pull_request` (`opened`, `synchronize`, `reopened`, `ready_for_review`) for PRs into `main`.
    It has `permissions: {}`, no secrets and no checkout. It only echoes a line. A branch can change it; nothing trusts it.
  - `review.yml` runs on `workflow_run` of "Review trigger", type `completed`. GitHub always takes it from `main`.
- `review.yml` has two jobs:
  - `resolve`: no environment, no secrets, `contents: read` and `pull-requests: read`.
    It requires a `pull_request` run of this repository that succeeded, from a branch of this repository, not by Dependabot.
    The head SHA comes from `workflow_run.head_sha`, which GitHub sets. The PR number comes from `workflow_run.pull_requests`, or, when that list is empty, from the API (`commits/{sha}/pulls`).
    The job reads the PR from the API. `factory/review/resolve_pr.py` (unit-tested, run from `main`) requires:
    open, not draft, base `main` of this repository, head from this repository, head SHA equal to the run's SHA, author not Dependabot.
    It checks the formats (number digits only, SHAs 40 hex) before it writes its outputs. No artifact or PR text crosses into the next job.
    When several open PRs share the head SHA, the lowest number is reviewed. The check run attaches to the SHA, so all of them show it.
  - `score-pr`: runs only when `resolve` found a PR. It holds the reviewer secrets.
- Any branch can add a workflow named "Review trigger", or change the real one. That only starts `review.yml`, which then reviews a real PR head commit or does nothing.
- The reviewer secrets are environment secrets of `reviewer`, which admits the `main` branch only (`infra/github/environments.tf`).
  Repository secrets would reach any `pull_request` workflow a branch adds. A `pull_request` job runs on `refs/pull/N/merge`, so the environment rejects it.
  A `workflow_run` job has `GITHUB_REF` set to `main`, so the environment admits it. `deploy.yml` already uses this with the `production` environment, which also admits `main` only.
- A fork PR gets no review: `resolve` rejects it, so the required check stays missing and the owner reviews it.
- Workspace root: `main` at `GITHUB_SHA`, the commit the workflow file came from. Everything the job executes or reads as instructions comes from there:
  the gate script, schema, skill, rubric, settings and the current rollout stage.
- The job uses no local `./` actions. A PR can change `factory/` on its branch, and CODEOWNERS blocks only the merge, not this run.
- The PR head is checked out into a separate directory as data. `.git` and every symlink are removed. Nothing from it runs: no install, no build, no hooks.
- The PR title and body come from the API into a file that Claude reads. No `run:` block expands a PR-controlled string.
- Claude Code runs as `claude -p --safe-mode --restricted --tools "Read,Grep,Glob" --strict-mcp-config --json-schema <schema>`, with `--max-turns` and `--max-budget-usd`.
  The exact command line is in `review.yml`.
  - Until CHB-105 it ran with `--bare`. `--bare` reads only `ANTHROPIC_API_KEY` or an `apiKeyHelper`, never an OAuth token, so it cannot use the subscription token (decision 7).
  - Each isolation `--bare` gave is replaced explicitly, most of them twice:

    | PR-controlled input | Blocked by |
    |---|---|
    | `CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md`, `AGENTS.md`, `.claude/rules/`, at the root of `head/` or nested | `--safe-mode`, `--restricted`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS=1` |
    | Hooks in `.claude/settings.json` | `--safe-mode`, `--restricted`, `disableAllHooks` in `--settings` |
    | Skills and agents in `.claude/` | `--safe-mode`, `--restricted`, and no `Skill` or `Agent` tool in `--tools` |
    | `.mcp.json` | `--strict-mcp-config`, `--safe-mode` |
    | Auto memory | `--safe-mode`, `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`, `autoMemoryEnabled: false` |
    | User settings, plugins, keychain or credentials files | Empty `HOME` and `CLAUDE_CONFIG_DIR` in the job; `--restricted` loads only `--settings` and managed settings |
    | The owner's claude.ai connectors (Gmail, Drive, others), reachable with a subscription token | `--strict-mcp-config`, `ENABLE_CLAUDEAI_MCP_SERVERS=false`, `disableClaudeAiConnectors` |
    | Background traffic | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` |

  - Tested on 2026-11-06 with the pinned CLI (2.1.311) and canary files in `head/`.
    A `CLAUDE.md`, `CLAUDE.local.md`, `.claude/CLAUDE.md`, `AGENTS.md`, rules, skills and agents each told the model to print a token,
    and a `.claude/settings.json` held hooks that touch marker files.
    Without the new flags (the old command minus `--bare`), 8 canary tokens reached the session after the model read `head/sub/code.py` and `head/sub2/code.py`.
    With the working directory at `head/`, all six hooks fired.
    With them, no token reached the model and no hook fired, also with the working directory at `head/` and after the model read files next to the nested canaries.
    `--safe-mode` alone and `--restricted` alone each blocked every canary.
    `CLAUDE_CODE_DISABLE_CLAUDE_MDS` alone left skills and hooks active, and `disableAllHooks` alone left every instruction file active.
  - `--tools` restricts the tool set. `--allowedTools` only pre-approves, so it is not enough. `--tools` does not remove MCP tools; the MCP switches above do.
  - The model has no shell, no network tool and no GitHub tool.
- The reviewer App token is minted in a later step, after Claude exits. The Claude process never sees it or a GitHub token.
- The `review` check run goes on the PR head SHA, so it shows on the PR. The Actions jobs of `review.yml` attach to `main`'s commit, not to the PR.
- CLI version and every action are pinned by commit, as in the existing workflows.

### 3. Typed result and the gate

- The schema extends CHB-62: `{scores: {codeQuality, security, businessAlignment, tests, architecture}, findings: [{id, severity, dimension, file, line, summary, suggestedFix}], acEvidenceChecked: [acId]}`.
- A tested script (`factory/`) validates the output and recomputes Merge Safety from the scores and findings. It never trusts a total from the model.
- Rule (CHB-40): fail if Merge Safety is below 8.5 or any finding is critical. A critical finding caps its dimension at 7.
- The script posts one check run named `review` with the reviewer App token, on the PR head SHA. Conclusion of a model review is `success` or `failure`.
- Before any model call, `factory/review/plan.py` (CHB-105, unit-tested, run from `main`) decides from the changed paths, the diff size and the App's earlier check runs.
  PR title, body and labels never count.
  - Docs only: every file changed since the merge base is on the docs allowlist. The check is `neutral` with the title "Skipped: docs-only change", and no model runs.
    The allowlist is Markdown and images (`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`) under `docs/`, and `README.md` files.
    Never docs: `CLAUDE.md`, `CLAUDE.local.md` and `AGENTS.md` at any depth, any dot directory,
    `factory/`, `infra/`, `scripts/`, `.github/`, `.claude/`, `docs/decisions/`, `docs/process/`, `docs/knowledge/`, `Makefile`, lockfiles and manifests.
  - Carry forward: the files changed between the last commit the App reviewed and the new head are all docs.
    The check repeats that verdict (`success` or `failure`) and its summary on the new head, and no model runs.
    The last reviewed commit comes from the `external_id` of the App's own check runs (`reviewed:<sha>`), never from a check of another app.
    If that commit is not an ancestor of the head, the model reviews.
    A run without a valid model review (Claude error, usage limit, schema violation) gets `error:<sha>` and is never carried: the next push gets a new review.
  - Skipped and carried checks have `external_id` `skipped` and `carried:<sha>`. They do not count as review rounds.
  - GitHub treats `neutral` as success for a required check. Once `review` is required, a docs-only PR passes it with no model review. The governance exclusions above are what keep that safe.
- Fail closed: schema error, timeout, budget cap, refusal or missing output give `failure`.
- Findings go to the check summary and inline review comments as plain text. Mentions are escaped and length is capped.
- The ruleset requires `review` with `integration_id` set to the reviewer App. A job named `review` in any workflow cannot satisfy it.

### 4. Risk classes and who approves

The trusted job computes the class from `git diff --no-renames --name-only base...head`.
Without `--no-renames`, a file moved out of `factory/` shows only its new path. Labels, titles and PR text do not count.

| Class | Paths | Approver |
|---|---|---|
| Harness | `.github/`, `.claude/`, `factory/`, `infra/`, `scripts/`, `Makefile`, `CLAUDE.md`, `docs/decisions/`, `docs/knowledge/`, the review skill and schema | Owner, always (CODEOWNERS) |
| Supply chain | Manifests and lockfiles, `.npmrc`, `pyproject.toml`, `Dockerfile`, `compose.yaml` | Owner, through the required `supply-chain` check; Dependabot-only PRs pass it. `.npmrc` stays in CODEOWNERS. See ADR-ebe6 (CHB-90). |
| Dependency | Dependabot PRs | ADR-ebe6 rules |
| Docs | `docs/specs/`, `docs/postmortems/`, `docs/sessions/`, `README.md` | Reviewer App, from stage 2 |
| Tests only | `**/tests/**`, `e2e/**` | Owner. A weakened test passes CI by construction. |
| Feature | Everything else | Owner, until stage 3 |

- A PR that spans classes takes the strictest one.
- `docs/decisions/` and `docs/knowledge/` steer the agents, so they are harness. Otherwise an agent could rewrite its own rules unattended.
- `.npmrc` and `pyproject.toml` hold the release-age exemptions of ADR-ebe6. A feature PR must not switch a supply-chain control off.
- CODEOWNERS lists the harness paths with the owner. It has no `*` line, or every PR would need the owner.
- The reviewer App submits an `APPROVE` review only when the check passes and the class is unattended at the current stage. Otherwise it submits a `COMMENT` review.
- `required_review_thread_resolution` is not a gate. The author can resolve threads.

### 5. Bounded loop

- Review, triage (CHB-60), fix, review again. At most 3 rounds (CHB-63).
- The round count comes from the reviewer App's own check runs on the PR, not from the PR body.
- After the third failing round, or on any blocking question, the ticket moves to Needs human and the notifier fires (CHB-48).
- A cap on total reviews per PR stops repeated pushes from fishing for a passing verdict.
- Findings are untrusted input for the author agent's triage step too.
- Owner override: the owner runs the review workflow with an `override` input. The gate script rejects it unless `github.actor` is the owner.
  The check then passes with "override by owner" in its summary. Overrides are counted.

### 6. Evidence and validation

- The PR body links one verify-ac screenshot per acceptance criterion (CHB-41, CHB-61). The reviewer checks that each AC id has evidence and records the ids in `acEvidenceChecked`.
- After merge, the ticket moves to Validating (ADR-ad3e). The deploy smoke test and the live Playwright run check it.
- A failed deploy or a fix-forward is recorded against the merged PR (CHB-76). This feeds change failure rate per risk class.

### 7. Model and cost

- Model, chosen by `plan.py` from paths and diff size only:
  - `claude-opus-5-5` when a changed path is harness, supply chain or governance (decision 3), when the diff has more than 400 added plus deleted lines, or when it changes a binary file outside `docs/`.
  - `claude-sonnet-5-5` otherwise.
- Credential (CHB-105): the owner's Claude subscription token from `claude setup-token`, as the environment secret `CLAUDE_CODE_OAUTH_TOKEN` of `reviewer`.
  The API key (CHB-89) ran on prepaid credits; 20 shadow runs cost USD 0.04 to 0.43 each.
  - The token shares the subscription's usage limits with the owner's interactive work and the local agent. A review that hits a limit fails, so its check is `failure`.
  - The token is valid for one year. Renewing it means running `claude setup-token` again and replacing the secret.
  - It reaches the claude.ai connectors of the owner's account. Decision 2 switches them off for the review.
- `--max-budget-usd 1` stops a runaway run. On a subscription the CLI still computes `total_cost_usd`, and a test with a cap of USD 0.001 ended with `error_max_budget_usd`.
- Docs-only PRs and docs-only pushes cost no model call (decision 3).
- Later: workload identity federation through GitHub OIDC, so no long-lived secret exists.
  `claude-code-action` documents it. The CLI docs checked here do not, so it stays unverified for the CLI.

### 8. Staged rollout

Each stage starts when the owner reads the metrics and agrees. The thresholds below are proposals.

| Stage | Unattended | Stays human | Evidence to move on |
|---|---|---|---|
| 0. Prerequisites | Nothing | Everything | CODEOWNERS on main; author App without `workflows: write`; reviewer App created |
| 1. Shadow | Nothing. The `review` check runs but is not required. | Every approval | 20 PRs: owner disagrees with the verdict on 2 or fewer; no critical finding missed that the owner found |
| 2. Required check; docs unattended | Docs PRs: reviewer approves, auto-merge | Tests, feature, harness | 20 docs merges with no revert; App approval confirmed to count |
| 3. Low-risk features | Feature PRs under a size limit, with AC evidence and no new dependency | Larger features, tests only, harness | Change failure rate of unattended merges no worse than owner-approved ones over 30 days |
| 4. Feature default | Most feature PRs | Harness, security-tagged tickets, fork PRs | Postmortems show no review-caused incident |

Harness paths stay human at every stage.

## Alternatives rejected

- ❌ `claude-code-action` as the gate: it adds bot-trigger checks (`allowed_bots`), comment posting, MCP GitHub tools and a default Claude GitHub App with workflows write.
  The CLI does the same review with less surface. The action stays useful for `@claude` requests later.
- ❌ The local orchestrator as reviewer: same machine and same 1Password as the author agent, so separation of duties is weak. It also stops when the laptop sleeps. Fine for experiments before stage 1.
- ❌ Claude Code Review (managed): it cannot gate. It needs a Team or Enterprise plan, and Anthropic chooses the model.
- ❌ Copilot code review as the gate: no score, no rubric control, not a required check.
  Copilot approvals can count toward required approvals, but approving on its own judgment skips the typed result. It stays as a second opinion.
- ❌ One App for author and reviewer: the author approves its own work. GitHub blocks self-approval only for the same identity.
- ❌ `pull_request` with repository secrets (the first version): any branch can run its own workflow copy with the secrets. Replaced by decision 2 in CHB-87.
- ❌ `pull_request_target` (the second version, CHB-87): GitHub blocks it on public repositories from 2026-12-02 unless an Actions event policy allows it, and this personal repository has no event policies.
  Replaced by `workflow_run` in CHB-93. The first rejection of `workflow_run` assumed an artifact handoff; the payload and the API give the PR number and head SHA without one.
- ❌ A `workflow_run` design that reads the PR number from an artifact the `pull_request` run uploads: the branch controls that run, so it controls the artifact.
- ❌ `required_approving_review_count = 0`, gating only on `review`: one compromised workflow run would merge feature code with no approval at all.
- ❌ Approval counted from a GitHub Actions job (`GITHUB_TOKEN`): any workflow can produce it, and merges by `GITHUB_TOKEN` start no CI.

## Owner decisions

Taken on 2026-11-06:

1. A second App, `chb-factory-reviewer`, whose check gates merges.
2. Remove `workflows: write` from `chb-factory-agent` after testing that Dependabot Actions bumps still merge. This amends ADR-d7b7.
3. CODEOWNERS covers the harness and supply-chain paths in the risk table (CHB-29), before any reviewer approval counts.
4. An Anthropic API key in its own Console workspace with a spend limit. Workload identity federation later, once verified for the CLI.
   Replaced by CHB-105: the owner's subscription token, with docs-only skips and the model chosen per PR (decision 7).
5. The stage thresholds in the rollout table, revisited with shadow-mode data.
6. The reviewer does not run on Dependabot PRs. Their release-age, provenance and CI checks gate them. The ruleset must not block their auto-merge.

## Consequences

- The owner still approves every PR until stage 2.
- Every PR needs a `review` check from the reviewer App once it is required. Fork PRs and runs without secrets block until the owner acts.
- Workflow changes by the agent need the owner's hands or a PR the owner pushes.
- The rubric, schema and skill live on `main` and change only through owner-reviewed PRs.
- Review spend comes out of the owner's subscription limits (CHB-105), tracked with CHB-56 and CHB-65.
- Once `review` is required, docs-only PRs pass it with a `neutral` check and no model review.
- The review needs no Actions event policy: it uses `pull_request` and `workflow_run`, not `pull_request_target`.
- A change to `review.yml` takes effect only after merge. Its own PR is reviewed by the version on `main`.
- A PR whose branch conflicts with `main` gets no review: GitHub runs no `pull_request` workflow without a merge commit.

## Unverified

- That the job authenticates with `CLAUDE_CODE_OAUTH_TOKEN` in an empty `HOME`.
  Local tests used the owner's logged-in session; a run with an empty `HOME` and no token failed closed (`loggedIn: false`, exit 1). AC7 of CHB-105 checks it after merge.
- Whether an App's `APPROVE` review counts toward `required_approving_review_count`. GitHub docs do not say. A community answer says it does. Test it before stage 2.
- Which OIDC claims an Anthropic federation rule can match beyond the repository subject prefix, and whether the bare CLI accepts federation.
- Whether an App token without `workflows` permission can merge a PR that changes workflow files, as Dependabot GitHub Actions bumps do.
  The auto-merge token in `dependabot-auto-merge.yml` requests only contents and pull requests today. Owner decision 2 depends on this.

## Sources

- Claude Code GitHub Actions: <https://code.claude.com/docs/en/github-actions>
- claude-code-action inputs and outputs: <https://github.com/anthropics/claude-code-action/blob/main/docs/usage.md>
- claude-code-action security: <https://github.com/anthropics/claude-code-action/blob/main/docs/security.md>
- Claude Code CLI flags (`--bare`, `--safe-mode`, `--restricted`, `--tools`, `--json-schema`, `--max-budget-usd`): <https://code.claude.com/docs/en/cli-reference>
- Claude Code environment variables (`CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_DISABLE_CLAUDE_MDS`, `ENABLE_CLAUDEAI_MCP_SERVERS`): <https://code.claude.com/docs/en/env-vars>
- Claude Code authentication, `claude setup-token`: <https://code.claude.com/docs/en/authentication>
- Claude Code settings (`disableAllHooks`, `disableClaudeAiConnectors`, `autoMemoryEnabled`): <https://code.claude.com/docs/en/settings-reference>
- Claude Code Review: <https://code.claude.com/docs/en/code-review>
- Claude model pricing: <https://claude.com/platform/api>
- About code owners: <https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/about-code-owners>
- Available rules for rulesets: <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets>
- About protected branches: <https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches>
- About status checks: <https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks>
- Secure use of GitHub Actions: <https://docs.github.com/en/actions/reference/security/secure-use>
- Events that trigger workflows: <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows>
- Securely using `pull_request_target`: <https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target>
- `pull_request_target` blocked by default from 2026-12-02 (2026-11-02): <https://github.blog/changelog/2026-11-02-pull_request_target-blocked-by-default-december-2>
- `workflow_run` event, `GITHUB_SHA` and `GITHUB_REF`: <https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_run>
- GitHub Security Lab, preventing pwn requests: <https://securitylab.github.com/resources/github-actions-preventing-pwn-requests/>
- `pull_request_target` and environment branch protections changes (2025-11-07): <https://github.blog/changelog/2025-11-07-actions-pull_request_target-and-environment-branch-protections-changes/>
- Workflow execution protections generally available (2026-09-17): <https://github.blog/changelog/2026-09-17-workflow-execution-protections-in-github-actions-generally-available/>
- Deployments and environments: <https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments>
- Permissions required for GitHub Apps: <https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps>
- Copilot code review: <https://docs.github.com/en/copilot/concepts/agents/code-review>
- Community answer on App approvals: <https://github.com/orgs/community/discussions/181487>
