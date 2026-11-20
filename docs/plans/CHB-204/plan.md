<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [CHB-204 plan: move the repository to `islomar-software-garden` and merge through a merge queue](#chb-204-plan-move-the-repository-to-islomar-software-garden-and-merge-through-a-merge-queue)
  - [Facts read on 2026-11-20](#facts-read-on-2026-11-20)
  - [Documentation this plan relies on](#documentation-this-plan-relies-on)
  - [Decisions for the human](#decisions-for-the-human)
  - [Unknowns](#unknowns)
  - [Costs](#costs)
  - [Must not be deleted or changed](#must-not-be-deleted-or-changed)
  - [Steps](#steps)
    - [(a) Preparation PRs: work before and after the transfer](#a-preparation-prs-work-before-and-after-the-transfer)
    - [(b) Human steps](#b-human-steps)
    - [(c) Post-transfer PRs and Terraform](#c-post-transfer-prs-and-terraform)
    - [(d) Local clones](#d-local-clones)
  - [Human time](#human-time)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# CHB-204 plan: move the repository to `islomar-software-garden` and merge through a merge queue

Status: draft for the human's approval. Nothing in this plan has run. Written 2026-11-20.

## Facts read on 2026-11-20

- Repository: `islomar/corporate-hotel-booking-software-factory`, id `1447565814`, human type `User`, public
  Source: `gh api repos/islomar/corporate-hotel-booking-software-factory`
- Organization: `islomar-software-garden`, id `359214981`, plan `free`, 0 App installations, base permission `read`, 2FA requirement off
  Source: `gh api orgs/islomar-software-garden`, `.../installations`
- Rulesets: `main` 26689871 (strict checks: backend, frontend, e2e, supply-chain, triage), `dependabot-branches` 26701623, `branch-named-tags` 27166103
  Source: `gh api repos/.../rulesets`
- Environments: `production`, `reviewer`, `agent-pr-updates` (Terraform), `copilot` (not in Terraform)
  Source: `gh api repos/.../environments`
- GitHub Apps: `chb-factory-agent`, `chb-factory-reviewer`: `GET /apps/<slug>` returns 404, so both are private
  Source: `gh api apps/chb-factory-agent`
- GitHub Terraform state: local file `infra/github/terraform.tfstate` (serial 47, 2026-11-15). `infra/github` has no `backend` and no `provider` block
  Source: `ls`, `git ls-files infra/github`
- GCP conditions: deployer: `assertion.repository=='islomar/...' && assertion.ref=='refs/heads/main'`;
  planner: `assertion.repository=='islomar/...' && assertion.event_name=='pull_request'`; both bindings on `attribute.repository/islomar/...`
  Source: `infra/gcp/identities.tf`, `planner.tf`, `variables.tf`
- Local clones: one `.git` shared by the main checkout and every worktree, `origin` = `git@github.com:islomar/corporate-hotel-booking-software-factory.git`
  Source: `git remote -v`, `git worktree list`

## Documentation this plan relies on

- GitHub, transferring a repository (<https://docs.github.com/en/repositories/creating-and-managing-repositories/transferring-a-repository>):
  - "If the transferred repository contains webhooks, services, secrets, or deploy keys, they will remain associated with the repository after the transfer is complete."
  - "All links to the previous repository location are automatically redirected to the new location." `git clone`, `git fetch` and `git push` to the old location also redirect.
  - The page "strongly recommend[s]" `git remote set-url origin NEW_URL`. A new repository or fork at the old location deletes the redirects for good.
  - "To transfer repositories to an organization, you must have permission to create repositories in the receiving organization."
  - The page says nothing about environments, environment secrets, Dependabot secrets, rulesets or App installations.
- GitHub, making an App public or private (<https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/making-a-github-app-public-or-private>):
  "If you set your GitHub App registration to private, it can only be installed on the account that owns the app."
- GitHub, transferring an App (<https://docs.github.com/en/apps/maintaining-github-apps/transferring-ownership-of-a-github-app>):
  Settings, Developer settings, GitHub Apps, the App, Advanced, Transfer ownership.
  "If the transfer would uninstall the app from your account, a warning appears." The page does not say whether the App ID, client ID and private keys are kept.
- GitHub, merge queue (<https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-a-merge-queue>):
  - "You must use the merge_group event to trigger your GitHub Actions workflow when a pull request is added to a merge queue."
  - "A merge queue will wait for required checks to be reported before it can proceed with merging."
  - "The merge queue provides the same benefits as the Require branches to be up to date before merging branch protection,
    but does not require a pull request author to update their pull request branch."
- GitHub, events that trigger workflows, `merge_group`: required checks need `merge_group` "as an additional trigger.
  Otherwise, status checks will not be triggered when you add a pull request to a merge queue. The merge will fail as the required status check will not be reported."
- `gh pr merge` manual (<https://cli.github.com/manual/gh_pr_merge>):
  "When targeting a branch that requires a merge queue, no merge strategy is required." "If required checks have not yet passed, auto-merge will be enabled."
  "If required checks have passed, the pull request will be added to the merge queue."
- GitHub OIDC reference (<https://docs.github.com/en/actions/reference/security/oidc>):
  - `repository_id`: "The ID of the repository from where the workflow is running." `repository_owner`: "The name of the organization in which the repository is stored."
  - "Repository renames and transfers after July 15, 2026 also move to the immutable subject format", for example `repo:octo-org@123456/octo-repo@456789:ref:refs/heads/main`.
- Google Cloud, Workload Identity Federation with deployment pipelines (<https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-deployment-pipelines>):
  - "Using "name" fields like `repository` and `repository_owner` increases the chances of cybersquatting." "To protect against this situation,
    use the numeric `*_id` fields instead, which are unique and can't be reused."
  - "Be sure to map any claims that you plan to use as attribute conditions or as part of a future `principalSet` condition."
  - The Azure DevOps section names 127 characters as "the maximum permitted length for `google.subject`".

## Decisions for the human

0. Confirm the Apps are private. The plan infers it from `GET /apps/<slug>` returning 404.
   Check: <https://github.com/settings/apps>, each App, Advanced. A "Make public" button means private.
1. App route.
   - ⭐ Route P (public first): make both Apps public, install them on the organization, then transfer the repository. No outage.
     Cost: anyone can see and install the two Apps on their own accounts. An install gives the Apps access to that installer's repositories, never the other way.
     After the move, transfer the Apps to the organization and make them private again (step B7).
   - ❌ Route T (transfer the Apps after the repository): the Apps stay private.
     From the repository transfer until the App transfer and the organization install, the agent cannot push or open PRs, and `pr-updates`, Dependabot auto-merge and the reviewer fail.
     The dry run shows this window as declared (`APP_ROUTE=transfer`). Expect 10 to 20 minutes if done in one sitting.
   - ❌ Create new Apps in the organization: new App IDs, client IDs and keys, new 1Password items and five secret updates. More work for the same result.
2. Split the ticket. CHB-204 says "one PR, about a day". This plan needs 7 PRs and 5 human sessions. ⭐ Split into child tickets:
   (1) prep code PR, (2) GCP expand, (3) post-transfer rename and provider account, (4) merge queue ruleset and CHB-177 close-out, (5) cleanup (GCP contract, `pr-updates`,
   ADRs). ❌ One ticket with seven PRs breaks "one ticket per branch".
3. Acceptance criterion 5 reads "no tracked file outside `docs/sessions/` names the old repository human".
   `islomar` is also the human's login (CODEOWNERS, fixtures, the human approval of `supply-chain`), the Jira site `islomar.atlassian.net` and the contact email.
   ⭐ Reword it to "names `islomar/corporate-hotel-booking-software-factory`".
   ❌ Keeping it literal would force renaming the Jira site and dropping the human from CODEOWNERS.

## Unknowns

- U1: Do environments, environment secrets and Dependabot secrets survive the transfer? The docs name "secrets" only.
  How it is settled: B5 reads the names after the transfer. If any are gone, the human re-adds the values from 1Password.
  Blocks: C3
- U2: Does an App transfer keep the App ID, client ID and keys? `as_agent.CLIENT_ID` and 5 secrets depend on them.
  How it is settled: B7 runs only after the canary. Read the App page after the transfer. Route P makes B7 optional.
  Blocks: B7
- U3: Can the Apps be installed on the organization with "Only select repositories" while it holds no repository?
  How it is settled: B2: if the UI demands a repository, pick "All repositories" (the organization has only this one), then narrow to the repository after B3.
  Blocks: B2
- U4: Does `gh pr merge --auto --squash` work on a queue branch, as called by `as_agent.py` and `dependabot-auto-merge.yml`?
  The manual says no strategy is needed and does not say a strategy is refused.
  How it is settled: C4 canary PRs. If it errors, C5 drops `--squash` in both places.
  Blocks: C4
- U5: Does the `merge_queue` block exist in provider `hashicorp/github` 6.13.0? `strings` on the binary finds `merge_queue`, `grouping_strategy`, `check_response_timeout_minutes`, which is not proof.
  How it is settled: C3 PR: `terraform validate` in the guard tests (CI `backend`).
  Blocks: C3
- U6: Does a strict required-checks policy still block entry to the queue for a branch behind `main`?
  How it is settled: C3 sets `strict_required_status_checks_policy = false`; the queue gives the same guarantee (docs quote above).
  Blocks: none
- U7: Does changing `attribute_mapping` and `attribute_condition` update the workload identity providers in place?
  How it is settled: A5 plan output must show "update in-place". `infra_guard` refuses a replace; `prevent_destroy` refuses a destroy.
  Blocks: A5
- U8: Do open PRs keep auto-merge after the transfer?
  How it is settled: B5 lists open PRs with `autoMergeRequest`. Re-enable with `factory/bin/as-agent gh pr merge --auto <url>` if lost.
  Blocks: none
- U9: `infra/README.md` says the GitHub state lives in `gs://chb-factory-a92932-tfstate/github`, but `infra/github` has no backend block and a local state file exists. Which is true?
  How it is settled: Human answer. The plan backs up the local file (B0) either way. A follow-up ticket moves it to GCS or fixes the README.
  Blocks: none
- U10: Organization Actions policy (Settings, Actions, General) may restrict the repository's allow list or workflow permissions.
  How it is settled: B5 GET `orgs/islomar-software-garden/actions/permissions`.
  Blocks: C1
- U11: Is the transfer reversible under the old name? The transfer doc says that after heavy GitHub Actions use in the week before,
  GitHub permanently retires the account and repository name combination. This repository runs CI on every push, so expect retirement.
  How it is settled: human accepts B3 as one-way for the name. The rollback of B3 is a forward fix in the organization.
  Blocks: B3

## Costs

None found. The organization is on the free plan. Public repositories get GitHub Actions minutes at no charge.
The merge queue adds one CI run per merge group; on a public repository that costs nothing. Google Cloud changes are IAM only.

## Must not be deleted or changed

- Never create a repository or fork named `islomar/corporate-hotel-booking-software-factory` after the transfer. It deletes every redirect for good.
- Do not delete `infra/github/terraform.tfstate` or its backup. It is the only GitHub state (U9).
- Do not delete the `copilot` environment. Terraform does not manage it, and it is not part of this ticket.
- Do not delete the existing GCP bindings `google_service_account_iam_member.github_main_impersonates_deployer` and `github_pr_impersonates_planner` before C6.
  They keep deploys working until the ID bindings are proven.
- Do not delete or recreate the workload identity pools or providers (`github`, `github-pr`). They have `prevent_destroy`.
- Do not delete the old App installations on `islomar` before B7. Before B3 they carry the agent.
- Do not run `make infra-plan` or `make infra-apply` between B3 and the apply of C1: the GitHub provider has no `owner` and would target `islomar`.
- Do not delete the target organization `islomar-software-garden`, and do not change its plan (free).

## Steps

Order: A1 to A5, B0 to B3, D1, B5, C1 (its merge is B6), C3 to C6, B7 last. C2 does not exist. `dry-run.sh` checks this order.

### (a) Preparation PRs: work before and after the transfer

A1. Agent PR: the human human is a constant, not the repository human.

- Actor: agent. Branch `CHB-<n>-owner-login`. `factory/bin/as-agent gh pr create`.
- Change: `supply-chain.yml` passes `--owner islomar` (or a repository variable `OWNER_LOGIN` managed in `infra/github/repository.tf`) instead of `${{ github.repository_owner }}`.
  `factory/review/agreement.py` line 150 and `factory/hooks/validating_sweep.py` line 312 use the same constant instead of the repository human.
  Unit tests in `scripts/tests/test_check_supply_chain.py`, `factory/tests/test_review_agreement.py`, `factory/tests/test_validating_sweep.py` cover an organization-owned answer.
- Why: after the transfer, `github.repository_owner` is `islomar-software-garden`.
  The human-approval path of the required `supply-chain` check could never pass, and the review agreement report would count no reactions.
- Verify: `.devcontainer/run make check` exits 0; the PR's `supply-chain` check passes.
- Roll back: revert the squash commit.

A2. Agent PR: workflow guards on the repository ID.

- Change: `pr-updates.yml` line 42 and `dependabot-auto-merge.yml` line 36:
  `github.repository_id == '1447565814'` (GitHub's context docs list the ID as a string) instead of the full name.
- Verify: the next push to `main` runs `PR updates` (not skipped); the next Dependabot PR runs the `triage` job of `Dependabot auto-merge`.
- Roll back: revert.

A3. Agent PR: required checks report on `merge_group`.

- Change:
  - `ci.yml`: `on: merge_group:` added. The `changes` job already reports `docs_only=false` for any event other than `pull_request`, so every test job runs.
  - `supply-chain.yml` and `triage.yml`: `on: merge_group:` added, and a step that exits 0 for `merge_group` before any step that reads `github.event.pull_request`.
    A PR enters the queue only after these checks passed on the PR.
  - `triage.yml` concurrency: `triage-${{ github.event.pull_request.number || github.ref }}`, so queue groups do not cancel each other. Same for `supply-chain.yml`.
  - `deploy.yml` needs no change: its gate requires `workflow_run.event == 'push'`.
- Verify: zizmor and `make check` pass. Each job name stays `backend`, `frontend`, `e2e`, `supply-chain`, `triage`.
- Roll back: revert. No queue exists yet, so nothing depends on it.

A4. Agent PR: `as_agent.py` accepts both names.

- Change: `REPOSITORY_CANDIDATES = ("islomar-software-garden/corporate-hotel-booking-software-factory", "islomar/corporate-hotel-booking-software-factory")`.
  `installation_token` tries `GET /repos/<name>/installation` for each, takes the first 200, and passes that name to the token request.
  `as_agent.REPOSITORY` stays the personal name, which `triage_review.py` uses; after the transfer it works through GitHub's redirect
  until C1 (CHB-221) switches it to the new name. Unit tests for 404 then 200.
- Why: the API 301 redirect for the old name is not documented for App JWT calls. This removes the dependency.
- Verify: `make check`; `factory/bin/as-agent gh api repos/{owner}/{repo} --jq .full_name` still prints the old name before B3.
- Roll back: revert.

A5. Agent PR, then human apply: GCP trusts the repository ID (expand).

- Change in `infra/gcp/`:
  - `variables.tf`: `github_repository_id = "1447565814"`.
  - `identities.tf` and `planner.tf`: add `"attribute.repository_id" = "assertion.repository_id"` to both `attribute_mapping`s.
    Conditions become `assertion.repository_id=='1447565814' && assertion.ref=='refs/heads/main'` and `assertion.repository_id=='1447565814' && assertion.event_name=='pull_request'`.
  - New bindings beside the old ones:
    `google_service_account_iam_member.github_main_impersonates_deployer_by_id` and `github_pr_impersonates_planner_by_id`,
    member `principalSet://iam.googleapis.com/<pool>/attribute.repository_id/1447565814`.
    The old bindings stay untouched (changing `member` forces a replace).
  - `guards.tf` locals and `tests/guards.tftest.hcl` follow the new conditions.
- Human apply, from any folder:

  ```sh
  cd ~/workspace/corporate-hotel-booking-software-factory && git switch main && git pull --ff-only && make infra-plan
  ```

  Check: GCP shows 2 to add (the `_by_id` bindings) and 2 to change in place (the two providers), 0 to destroy; GitHub shows no change. Then:

  ```sh
  make -C ~/workspace/corporate-hotel-booking-software-factory infra-apply
  ```

- Verify: the next push to `main` deploys (Actions, Deploy, green). The next PR that touches `infra/gcp/` gets a `Terraform plan` comment.
- Roll back: revert the PR and apply again. The old bindings never left, so a revert restores the name condition with no gap.
- Time: human 15 minutes.

### (b) Human steps

B0. Back up the GitHub state.

- Actor: human. 1 minute.

  ```sh
  cp -p ~/workspace/corporate-hotel-booking-software-factory/infra/github/terraform.tfstate ~/workspace/corporate-hotel-booking-software-factory/infra/github/terraform.tfstate.pre-CHB-204
  ```

- Verify: `ls -l ~/workspace/corporate-hotel-booking-software-factory/infra/github/terraform.tfstate*` shows both files with the same size.
- Roll back: none needed.

B1. Make both Apps public (Route P).

- Actor: human. 5 minutes.
- Click path, for `chb-factory-agent` and then `chb-factory-reviewer`: <https://github.com/settings/apps>, the App, Advanced, "Make public", confirm.
- Verify: `gh api apps/chb-factory-agent --jq .owner.login` prints `islomar` (it returned 404 while private). Same for the reviewer.
- Roll back: Advanced, "Make private". Allowed while the App is installed only on `islomar`.

B2. Install both Apps on the organization.

- Actor: human. 5 minutes.
- Click path: <https://github.com/apps/chb-factory-agent>, Install, account `islomar-software-garden`, "All repositories" (U3), Install. Same for <https://github.com/apps/chb-factory-reviewer>.
- Verify: `gh api orgs/islomar-software-garden/installations --jq '.installations[].app_slug'` lists both.
- Roll back: <https://github.com/organizations/islomar-software-garden/settings/installations>, the App, Configure, Uninstall.

B3. Transfer the repository.

- Actor: human. 5 minutes.
- Before: no workflow run in progress on `main` (`gh run list -R islomar/corporate-hotel-booking-software-factory --branch main --limit 5`). Every A PR merged; A5 applied.
- Click path: <https://github.com/islomar/corporate-hotel-booking-software-factory/settings>, General, Danger Zone, "Transfer", "Select one of my organizations",
  `islomar-software-garden`, type `islomar/corporate-hotel-booking-software-factory` to confirm, "I understand, transfer this repository".
- Changes: repository human, full name, every URL. The repository ID stays `1447565814`.
- Verify: `gh api repositories/1447565814 --jq '.full_name, .owner.type'` prints `islomar-software-garden/corporate-hotel-booking-software-factory` and `Organization`.
  `curl -sI https://github.com/islomar/corporate-hotel-booking-software-factory | head -1` prints a 301.
- Roll back: one-way for the name (U11). GitHub may retire `islomar/corporate-hotel-booking-software-factory` for good.
  Fix forward in the organization instead: every A change already accepts the new repository human.
  A transfer back to `islomar` is possible from the organization's repository Settings, Danger Zone, Transfer, but may not get the old name back.

D1 (from group d, run right after B3): see below.

B5. Check what survived the transfer (agent reads, human fixes).

- Actor: agent (GET only), human for any fix. 10 minutes.
- Reads, names only, never values:

  ```sh
  R=repos/islomar-software-garden/corporate-hotel-booking-software-factory
  gh api $R/environments --jq '.environments[].name'
  for e in production reviewer agent-pr-updates; do gh api $R/environments/$e/secrets --jq "\"$e: \" + ([.secrets[].name] | join(\" \"))"; done
  gh api $R/dependabot/secrets --jq '.secrets[].name'
  gh api $R/actions/variables --jq '.variables[].name'
  gh api $R/rulesets --jq '.[].name'
  gh api $R/labels --paginate --jq '.[].name'
  gh api $R/installation --jq .app_slug
  gh api orgs/islomar-software-garden/actions/permissions
  gh pr list -R islomar-software-garden/corporate-hotel-booking-software-factory --json number,autoMergeRequest
  ```

- Expect: 4 environments; `production` with no secret, `reviewer` with `CLAUDE_CODE_OAUTH_TOKEN`, `REVIEWER_APP_CLIENT_ID`, `REVIEWER_APP_PRIVATE_KEY`;
  `agent-pr-updates` with `AGENT_APP_CLIENT_ID`, `AGENT_APP_PRIVATE_KEY`;
  Dependabot secrets `AGENT_APP_CLIENT_ID`, `AGENT_APP_PRIVATE_KEY`; 5 variables `APP_URL`, `GCP_*`; 3 rulesets; the 5 managed labels.
- Fix if missing: the human re-adds the value from 1Password in Settings, Environments (or Secrets and variables, Dependabot).
- Then: `factory/bin/as-agent gh api repos/{owner}/{repo} --jq .full_name` prints the new name.

B6. A push to `main` deploys.

- Actor: human approves the first post-transfer agent PR (C1). Its merge is the deploy check.
- Verify: `gh run list -R islomar-software-garden/corporate-hotel-booking-software-factory --workflow deploy.yml --limit 1` shows success, and the `deploy` job log shows the Google auth step passing.
- If it fails with a workload identity error: check `google.subject` length (dry run computes 113 characters for `environment:production`) and the A5 conditions.

B7. Optional, last: give the Apps to the organization and make them private again.

- Actor: human. 10 minutes. Only after C4 passed.
- Click path: <https://github.com/settings/apps>, the App, Advanced, "Transfer ownership", `islomar-software-garden`, "Transfer this GitHub App". Then in the organization:
  Settings, Developer settings, GitHub Apps, the App, Advanced, "Make private".
- Verify: `factory/bin/as-agent gh api repos/{owner}/{repo} --jq .full_name` still works; the App page still shows client ID `Iv23li3OLZUlWY7ZMLdr` (U2).
- Roll back: transfer back to `islomar`.

### (c) Post-transfer PRs and Terraform

C1. Agent PR, human apply: new name everywhere, GitHub provider account.

- Change:
  - New `infra/github/versions.tf`: `provider "github" { owner = "islomar-software-garden" }`.
  - `infra/github/tests/guards.tftest.hcl`: `full_name` override with the new name.
  - `factory/as_agent.py`: new name first (A4 keeps the fallback until C6). `factory/jira.py` `PULL_URL`, `factory/tests/test_as_agent.py`, `factory/tests/test_jira.py`.
  - `README.md` badges.
  - `infra/gcp/variables.tf` `github_repository`: unchanged in this PR; it still names the old bindings. C6 removes it.
- Human apply: `make infra-plan` from updated `main`. Expected GitHub plan: no add, no destroy; at most in-place updates of computed fields. Then `make infra-apply`.
- Verify: `git grep -n "islomar/corporate-hotel-booking-software-factory" -- ':!docs/sessions'` lists only `infra/gcp` (old bindings, removed in C6) and ADR history.
- Roll back: revert; the redirects keep the old links working.

C3. Agent PR, human apply: the `main` ruleset requires a merge queue.

- Change in `infra/github/ruleset.tf`: a `merge_queue` block in `rules` (merge method `SQUASH`, grouping `ALLGREEN`, build concurrency 1, min 1, max 5 entries,
  wait 5 minutes, check timeout 60 minutes).
  `strict_required_status_checks_policy = false` (U6). The guard test `main_ruleset_requires_up_to_date_branches` becomes `main_ruleset_requires_a_merge_queue`. ADR-2ba2 amended.
- Human apply: `make infra-plan`, expect 1 change in place (`github_repository_ruleset.main`), then `make infra-apply`.
- Verify: `gh api repos/islomar-software-garden/corporate-hotel-booking-software-factory/rules/branches/main --jq '.[].type'` lists `merge_queue`.
- Roll back: revert and apply. The PRs fall back to the strict policy.

C4. Canary: one Dependabot PR and one agent PR merge through the queue.

- Dependabot: the next safe update (`dependencies:auto-merge` label) joins the queue and merges with no person acting.
- Agent: the next agent PR merges after the human's approval, with no rebase.
- Verify: `gh api graphql` on the PR timeline shows `AddedToMergeQueueEvent` then `MergedEvent`; the push to `main` deploys.
- If `gh pr merge --auto --squash` errors (U4): drop `--squash` in `as_agent.py` and `dependabot-auto-merge.yml`.
- Human time: 5 minutes for one approval.

C5. Agent PR: retire the rebase logic.

- Change: `factory/pr_updates.py` and `pr-updates.yml` keep only the `needs-rebase` label on conflicting agent PRs; the Dependabot rebase comment and agent rebases go.
  `factory/README.md`, `docs/process/delivery-pipeline.md`, ADR-d7b7 (new repository human, Apps installed on the organization) and ADR-2ba2 amended.
  CHB-177 and CHB-172 Validating sections close.
- Verify: `make check`; next push to `main` logs only conflict checks.
- Roll back: revert.

C6. Agent PR, human apply: GCP contract.

- Change: remove `google_service_account_iam_member.github_main_impersonates_deployer`, `github_pr_impersonates_planner`, their `import` blocks in `imports.tf`,
  `variable "github_repository"` and the `attribute.repository` mapping if unused.
  Remove the old name from `as_agent.REPOSITORY_CANDIDATES`.
- Human apply:

  ```sh
  make -C ~/workspace/corporate-hotel-booking-software-factory infra-plan ALLOW_DESTROY='google_service_account_iam_member.github_main_impersonates_deployer,google_service_account_iam_member.github_pr_impersonates_planner'
  make -C ~/workspace/corporate-hotel-booking-software-factory infra-apply ALLOW_DESTROY='google_service_account_iam_member.github_main_impersonates_deployer,google_service_account_iam_member.github_pr_impersonates_planner'
  ```

  Do not destroy the `_by_id` bindings, the pools or the providers.
- Verify: the next push to `main` deploys.
- Roll back: revert and apply; the bindings come back.

### (d) Local clones

D1. Point `origin` at the new name, right after B3.

- Actor: human. 1 minute. One `.git` serves every worktree.

  ```sh
  git -C ~/workspace/corporate-hotel-booking-software-factory remote set-url origin git@github.com:islomar-software-garden/corporate-hotel-booking-software-factory.git
  git -C ~/workspace/corporate-hotel-booking-software-factory fetch origin
  ```

- Verify: `git -C ~/workspace/corporate-hotel-booking-software-factory remote -v` shows the new URL; `fetch` exits 0.
- Roll back: the same command with the old URL. The redirect makes both work.

## Human time

| Step | Minutes |
|---|---|
| A5 GCP apply | 15 |
| B0 back up state | 1 |
| B1 Apps public | 5 |
| B2 install on organization | 5 |
| B3 transfer | 5 |
| D1 remote | 1 |
| B5 fix missing secrets, if any | 0 to 15 |
| C1 apply | 10 |
| C3 apply | 10 |
| C4 approve canary | 5 |
| C6 apply | 10 |
| B7 Apps to organization (optional) | 10 |
| Total | about 80 to 95, over several days of PR review |
