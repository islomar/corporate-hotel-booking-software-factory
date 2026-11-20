<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Infrastructure](#infrastructure)
  - [Use](#use)
    - [Destroy and replace](#destroy-and-replace)
    - [Guard tests](#guard-tests)
    - [Move the `github` state to the bucket](#move-the-github-state-to-the-bucket)
  - [Planner identity](#planner-identity)
    - [Plan on pull requests](#plan-on-pull-requests)
  - [Rules](#rules)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Infrastructure

Terraform manages the infrastructure. See ADR-aae8.

| Folder | Scope | Credentials | State |
|---|---|---|---|
| `gcp/` | Google Cloud: APIs, registry, service accounts, Workload Identity (deployer from `main`, read-only planner for pull requests), budget, Cloud Run, monitoring (uptime check, SLO, alert policies, email channel) | your gcloud login (configuration `chb`) | `gs://chb-factory-a92932-tfstate/gcp` |
| `github/` | Repository settings, Actions variables and permissions (allow list, SHA pinning), the `main` ruleset, deployment environments, the Dependabot labels, the Jira autolink, and guards for what it cannot manage (`guards.tf`) | your `gh auth token` (admin) | `gs://chb-factory-a92932-tfstate/github` |

The state bucket was created by hand (CHB-10) and is imported into `gcp/`.

## Use

Requirements: [tfenv](https://github.com/tfutils/tfenv) (reads `.terraform-version`), gcloud with the `chb` configuration.

The alerts go to an email address that is not in the repository.
Export it in the shell that runs `make infra-plan`, for example from 1Password:

```sh
export TF_VAR_alert_email="$(op read 'op://<vault>/<item>/<field>')"
```

Never write the address in a file that git tracks, dotfiles included.

`make infra-plan` stops without it. `make infra-apply` reads it from the saved plan.

CI plans and never applies (see [Plan on pull requests](#plan-on-pull-requests)). The human applies locally, after the PR with the Terraform change merges:

1. Update `main`: `git switch main && git pull --ff-only`.
2. Run `make infra-plan`. It writes `infra/.plans/gcp.tfplan` and `infra/.plans/github.tfplan`, and prints a summary per folder: add, change, destroy, replace and forget counts.
3. Read the plan output and the summary. They must match the merged PR.
4. Run `make infra-apply`. It applies the saved plans, nothing else, and deletes them when the apply succeeds.

`scripts/infra_guard.py` stops both targets when one of these is true. It checks before it asks gcloud for a token.

- the checkout is not `main`;
- `main` is behind or ahead of `origin/main` (it runs `git fetch` first);
- `infra/`, `Makefile` or `scripts/` have uncommitted or untracked files.

Changes in other folders, for example `frontend/` or untracked notes, do not stop it.
The message names the fix command.

`make infra-apply` also stops when a saved plan is missing, comes from another commit, or changed after `make infra-plan` wrote it.
Run `make infra-plan` again.

### Destroy and replace

A plan that destroys or replaces a resource fails both targets, in red, with the addresses.
On 2026-11-06 a plan from an outdated branch showed 4 destroys of live resources (CHB-99).
An unexpected destroy almost always means the checkout misses merged code. Do not override it.

When you intend every destroy, list the exact addresses, comma-separated, for both targets.
Use single quotes: addresses contain `"` and `[`.

```sh
make infra-plan ALLOW_DESTROY='github_repository_environment.env["staging"],google_x.y'
make infra-apply ALLOW_DESTROY='github_repository_environment.env["staging"],google_x.y'
```

The list must match exactly. A missing or an extra address fails.

### Guard tests

`github/guards.tf` fails the plan when a setting Terraform cannot manage is off, for example CodeQL default setup.
`github/tests/guards.tftest.hcl` checks each guard against stubbed GitHub responses. It needs no credentials.
`gcp/guards.tf` fails the plan when the planner holds a role outside a read-only allowlist or the deployer's `refs/heads/main` condition changes (CHB-81).
Both Workload Identity conditions trust the repository ID (`github_repository_id` in `gcp/variables.tf`), not its name (CHB-220).
GitHub keeps the ID when the repository moves to another owner or gets a new name.
The bindings on the old name stay until CHB-204 removes them.
`gcp/tests/guards.tftest.hcl` breaks each guard with a mocked Google provider.
`make check` and the `backend` job of CI run both folders (`scripts/check_guard_tests.py`, CHB-158, CHB-166). A PR that breaks a guard shows `backend` failing.
CI installs Terraform from releases.hashicorp.com, pinned by SHA-256 (`TERRAFORM_SHA256` in `.github/workflows/ci.yml`), and uses no third-party action and no credentials.
Both folders pin the same Terraform version. When you change a `.terraform-version`, change both, and update that hash from `terraform_<version>_SHA256SUMS` (the `linux_amd64` line) in the same PR.
The script needs Terraform at the version in each `.terraform-version`, network access to download the provider, and no credentials.
It removes GitHub and Google credentials from the environment and keeps `.terraform` out of the checkout.
Run it alone:

```sh
python3 -I ~/workspace/corporate-hotel-booking-software-factory/scripts/check_guard_tests.py
```

### Move the `github` state to the bucket

`github/backend.tf` keeps the state in `gs://chb-factory-a92932-tfstate/github` (CHB-216).
Before it, the state was the local file `infra/github/terraform.tfstate`, git-ignored, in the human's main checkout only.
Run these steps once, in that checkout, after the CHB-216 PR merges.
Do not run `make infra-plan` or `make infra-apply` until step 4 passes: their `github` init cannot copy the local state.
`make infra-apply` also refuses without a plan saved from the current `main`.
Never run `init -reconfigure` here: it starts an empty state in the bucket and leaves the real one behind.

1. Update `main` and back up the local state outside the repository:

   ```sh
   git -C ~/workspace/corporate-hotel-booking-software-factory switch main
   git -C ~/workspace/corporate-hotel-booking-software-factory pull --ff-only
   cp -p ~/workspace/corporate-hotel-booking-software-factory/infra/github/terraform.tfstate ~/chb-github-terraform.tfstate.pre-gcs
   ```

2. Copy the state to the bucket. Answer `yes` when Terraform asks to copy the existing state:

   ```sh
   export GOOGLE_OAUTH_ACCESS_TOKEN="$(CLOUDSDK_ACTIVE_CONFIG_NAME=chb gcloud auth print-access-token)"
   cd ~/workspace/corporate-hotel-booking-software-factory && terraform -chdir=infra/github init -migrate-state
   ```

3. Check that the bucket holds the state. The list shows `gs://chb-factory-a92932-tfstate/github/default.tfstate`:

   ```sh
   CLOUDSDK_ACTIVE_CONFIG_NAME=chb gcloud storage ls gs://chb-factory-a92932-tfstate/github/
   ```

4. Check that Terraform reads it. The plan must say `No changes.`:

   ```sh
   export GOOGLE_OAUTH_ACCESS_TOKEN="$(CLOUDSDK_ACTIVE_CONFIG_NAME=chb gcloud auth print-access-token)"
   export GITHUB_TOKEN="$(gh auth token)"
   cd ~/workspace/corporate-hotel-booking-software-factory && terraform -chdir=infra/github plan
   ```

5. Only when steps 3 and 4 pass, delete the local copies.
   Delete these three files and nothing else. Leave every file under `infra/gcp/` alone:

   ```sh
   rm -f ~/workspace/corporate-hotel-booking-software-factory/infra/github/terraform.tfstate
   rm -f ~/workspace/corporate-hotel-booking-software-factory/infra/github/terraform.tfstate.backup
   rm ~/chb-github-terraform.tfstate.pre-gcs
   ```

If step 3 or 4 fails, keep every file and report the output.
After step 5 the bucket is the only copy. It keeps every older version of a state object (versioning) and a deleted object for 7 days (soft delete).

## Planner identity

Pull request runs plan with a separate identity (CHB-81, ADR-3c1f), used by CHB-13.

| Item | Value |
|---|---|
| Pool and provider | `github-pr` / `github-pr`, trust `pull_request` runs of this repository only, by repository ID |
| Service account | `github-planner@<project>.iam.gserviceaccount.com` |
| Roles | `roles/viewer`, `roles/iam.securityReviewer`, `roles/serviceusage.serviceUsageConsumer`, `roles/storage.objectViewer` on the state bucket |

It reads and cannot write. It plans with `terraform plan -lock=false`, because the state lock is a write to the state bucket.
Its pool is separate from the deployer's: the deployer binding trusts the whole `github` pool.
Add a role only to `local.planner_role_allowlist` in `gcp/guards.tf` and `gcp/planner.tf`, with the human's approval.
Bind planner roles only in `gcp/planner.tf`. See ADR-3c1f for what the planner can and cannot do.

### Plan on pull requests

`.github/workflows/terraform-plan.yml` (CHB-13) runs for a pull request that changes a file under `infra/gcp/`.
It runs `terraform plan -lock=false` as the planner, and posts one comment on the pull request.
Each push updates that comment.
Pull requests from forks and pull requests without an `infra/gcp/` change do not run it.
It never applies.

The comment shows:

- the count of resources to add, change and destroy (a replace counts as one add and one destroy);
- the address and the action of each changed resource, for example `google_cloud_run_v2_service.chb` and `update`;
- the commit and a link to the run.

It hides everything else, because the repository is public:

- attribute values, the plan text and the state content (the state holds the alert address and the billing account ID);
- the plan itself: it is a file in `$RUNNER_TEMP` that is never printed or uploaded.

`scripts/plan_summary.py` reads the plan JSON and keeps only counts, addresses and actions. `scripts/tests/test_plan_summary.py` feeds it a plan with a sensitive value and checks that the value is absent.

A failed plan fails the job. The log shows the `Error:` lines of Terraform and nothing else, and the comment says the plan failed.
The `Error:` lines appear as Terraform writes them, cut to 400 characters, in a public log.
They can name resource IDs, for example a service account email or a bucket name. The job masks only `billing_account` and `alert_email`.
The human accepted this risk on 2026-11-15 (review of PR #125, finding 3): resource names are not secrets, and debugging a failure needs the full text.

The plan needs `billing_account` and `alert_email`. The job reads both from the state, masks them in the log, and passes them as `TF_VAR_*`. No repository secret holds them.
Limit: a pull request that changes one of the two values shows no change for it, because the plan compares against the applied value.

`terraform-plan.yml` pins Terraform like `ci.yml`: the version of `infra/gcp/.terraform-version`, checked against `TERRAFORM_SHA256`. When you bump the version, update the hash in both workflows.

## Rules

- IAM is additive only (`*_iam_member`). Never manage project IAM authoritatively: it would remove the human and Google's service agents.
- `prevent_destroy` protects the service accounts, the Workload Identity pool and provider, the registry, the state bucket and the Cloud Run service.
  Replacing a service account changes its ID and breaks Workload Identity.
- Secrets never go into Terraform: their values would be stored in the state.
- The billing account ID is a sensitive variable, filled from gcloud at run time. Never commit it.
- The deploy workflow changes only the Cloud Run image. Terraform ignores the image.
- A plan with an unexpected destroy or replace is a bug in the code. Do not apply it.
