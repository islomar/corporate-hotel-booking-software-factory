<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-3c1f: A read-only planner identity for pull requests](#adr-3c1f-a-read-only-planner-identity-for-pull-requests)
  - [Context](#context)
  - [Decision](#decision)
  - [What the planner can and cannot do](#what-the-planner-can-and-cannot-do)
  - [Why `plan -lock=false`](#why-plan--lockfalse)
  - [Alternatives rejected](#alternatives-rejected)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-3c1f: A read-only planner identity for pull requests

- Status: accepted
- Date: 2026-11-10

## Context

CHB-13 will show the effect of an `infra/gcp` change in the pull request, before merge.
A plan reads the live Google Cloud setup, so the workflow needs a credential.
The deployer is the wrong one: it trusts `refs/heads/main` only (ADR-d7b7) and it can change Cloud Run.
A pull request run executes code from a branch that an agent can edit, so its credential must not be able to change anything.

## Decision

- A second Workload Identity pool `github-pr` with one provider `github-pr` (`infra/gcp/planner.tf`).
  Its condition is `assertion.repository=='<repo>' && assertion.event_name=='pull_request'`.
- The pool is separate from `github`. The deployer binding trusts `attribute.repository` of the whole `github` pool.
  A second provider in that pool, with the same mapping, would let pull request runs impersonate the deployer.
- A service account `github-planner`, impersonated by that pool only.
- Its roles: `roles/viewer` and `roles/iam.securityReviewer` on the project, `roles/storage.objectViewer` on the state bucket.
- `infra/gcp/guards.tf` fails the plan when:
  - the planner holds a role outside that allowlist;
  - the deployer's condition is not exactly this repository on `refs/heads/main`;
  - the planner's condition changes;
  - the two providers share a pool.
- `infra/gcp/tests/guards.tftest.hcl` breaks each guard with a mocked provider and needs no credentials. `make check` runs it.
- `scripts/check_guard_tests.py` also fails when a file other than `planner.tf` and `guards.tf` names the planner service account or its pool, because a role bound there would skip the allowlist.

## What the planner can and cannot do

Can:

- read project resources, IAM policies and the Terraform state file;
- read from a pull request run of this repository, branch code included.

Cannot:

- create, change or delete any Google Cloud resource;
- write the state file or the state lock;
- run from `main`, a tag, a schedule or a fork. Forks get no OIDC token from GitHub;
- impersonate the deployer or the runtime service account.

## Why `plan -lock=false`

A plan takes the state lock by default. The lock is an object that Terraform writes in the state bucket, so locking needs write access to it.
The planner has read access only. `-lock=false` skips the lock and the planner stays read-only.
The cost: a plan can read state while an apply writes it, and show a stale diff. A plan never writes, so it cannot corrupt the state.
The apply keeps the lock.

## Alternatives rejected

- Plan with the deployer: it trusts `main` only, and widening that condition lets any branch deploy.
- A second provider in the `github` pool: the deployer binding would trust it too (see Decision).
- `roles/editor` or `roles/owner` for the planner: both write.
- Terraform Cloud or a state lock with write access: a new service, or write access in a pull request run.

## Consequences

- A pull request that edits a workflow can use the planner. It then reads project resources, IAM policies and the state.
  The state can hold sensitive values, for example the alert email address and the billing account ID.
  CHB-13 must keep plan output out of public logs and run plans only for the `infra/gcp` folder.
- `roles/viewer` may not cover every API the refresh calls. A plan can fail on a missing permission, for example the budget (read on the billing account) or `serviceusage.services.use`.
  Add the narrowest read-only role to the allowlist in `guards.tf` and `planner.tf`, with the owner's approval.
- The five `planner_*` and `deployer_ref` variables exist only so tests can break the guards. Any value but the default fails the plan.
- Amends ADR-d7b7: Google Cloud trusts GitHub OIDC for `refs/heads/main` (deployer) and, read-only, for `pull_request` runs (planner).
