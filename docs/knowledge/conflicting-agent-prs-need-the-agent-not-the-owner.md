<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [A conflicting PR runs no checks, and the owner's "Update branch" voids the owner's approval](#a-conflicting-pr-runs-no-checks-and-the-owners-update-branch-voids-the-owners-approval)
  - [A conflicting PR runs no `pull_request` workflow](#a-conflicting-pr-runs-no-pull_request-workflow)
  - [The owner's "Update branch" makes the owner the last pusher](#the-owners-update-branch-makes-the-owner-the-last-pusher)
  - [What the repository does](#what-the-repository-does)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# A conflicting PR runs no checks, and the owner's "Update branch" voids the owner's approval

Found on 2026-11-10 ([CHB-172](https://islomar.atlassian.net/browse/CHB-172)).
Two GitHub behaviors made agent PRs wait on the owner for no reason.

## A conflicting PR runs no `pull_request` workflow

GitHub builds the `pull_request` merge commit from the head and `main`.
With a merge conflict there is no merge commit, so no `pull_request` workflow starts.
PR #100 showed this on 2026-11-10: its checks page listed only CodeQL, green.
CodeQL runs on the head commit. `backend`, `frontend` and `e2e` never ran.

A green checks page on a conflicting PR proves nothing.
Look for the "This branch has conflicts" banner and the `needs-rebase` label first.

## The owner's "Update branch" makes the owner the last pusher

The `main` ruleset sets `require_last_push_approval` (`infra/github/ruleset.tf`).
The approval of the last push must come from someone other than the person who pushed.
The "Update branch" button pushes as the clicking user.
After the owner clicks it, the owner is the last pusher, and the owner's own approval no longer counts.
PR #102 stayed blocked on 2026-11-10 after four owner approvals for this reason.

A push by the agent App leaves the owner free to approve ([ADR-d7b7](../decisions/ADR-d7b7-agent-github-identity.md)).

## What the repository does

- `.github/workflows/pr-updates.yml` runs on every push to `main` as the agent App.
  It rebases an agent PR that is behind `main` and mergeable, approved or not.
  Since CHB-177 `strict_required_status_checks_policy` is true, so an approved PR behind `main` cannot merge. The rebase dismisses the approval and the owner approves the new head.
  It asks Dependabot to rebase its own PRs that are behind `main` with a `@dependabot rebase` comment.
  The decision logic is `factory/pr_updates.py`.
- A conflicting agent PR gets the label `needs-rebase`.
  The SessionStart sweep (`factory/hooks/validating_sweep.py`) lists these PRs as "resolve first".
  The agent rebases them locally, pushes through `factory/bin/as-agent` and removes the label.
- The owner does not click "Update branch" on an agent PR.
  If one has to be updated by hand, ask the agent.
