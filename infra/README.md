<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Infrastructure](#infrastructure)
  - [Use](#use)
    - [Destroy and replace](#destroy-and-replace)
    - [Guard tests](#guard-tests)
  - [Planner identity](#planner-identity)
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

Until CI applies (CHB-13), the owner applies locally, after the PR with the Terraform change merges:

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
`gcp/tests/guards.tftest.hcl` breaks each guard with a mocked Google provider.
`make check` runs it (`scripts/check_guard_tests.py`, CHB-158). CI does not run it yet.
The script runs both folders. It needs Terraform at the version in each `.terraform-version`, network access to download the provider, and no credentials.
It removes GitHub and Google credentials from the environment and keeps `.terraform` out of the checkout.
Run it alone:

```sh
python3 -I ~/workspace/corporate-hotel-booking-software-factory/scripts/check_guard_tests.py
```

## Planner identity

Pull request runs plan with a separate identity (CHB-81, ADR-3c1f), used by CHB-13.

| Item | Value |
|---|---|
| Pool and provider | `github-pr` / `github-pr`, trust `pull_request` runs of this repository only |
| Service account | `github-planner@<project>.iam.gserviceaccount.com` |
| Roles | `roles/viewer`, `roles/iam.securityReviewer`, `roles/storage.objectViewer` on the state bucket |

It reads and cannot write. It plans with `terraform plan -lock=false`, because the state lock is a write to the state bucket.
Its pool is separate from the deployer's: the deployer binding trusts the whole `github` pool.
Add a role only to `local.planner_role_allowlist` in `gcp/guards.tf` and `gcp/planner.tf`, with the owner's approval.
Bind planner roles only in `gcp/planner.tf`. See ADR-3c1f for what the planner can and cannot do.

## Rules

- IAM is additive only (`*_iam_member`). Never manage project IAM authoritatively: it would remove the owner and Google's service agents.
- `prevent_destroy` protects the service accounts, the Workload Identity pool and provider, the registry, the state bucket and the Cloud Run service.
  Replacing a service account changes its ID and breaks Workload Identity.
- Secrets never go into Terraform: their values would be stored in the state.
- The billing account ID is a sensitive variable, filled from gcloud at run time. Never commit it.
- The deploy workflow changes only the Cloud Run image. Terraform ignores the image.
- A plan with an unexpected destroy or replace is a bug in the code. Do not apply it.
