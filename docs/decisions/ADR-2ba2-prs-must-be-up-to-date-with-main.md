<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-2ba2: A PR merges only when it contains the latest main](#adr-2ba2-a-pr-merges-only-when-it-contains-the-latest-main)
  - [Context](#context)
  - [Decision](#decision)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-2ba2: A PR merges only when it contains the latest main

- Status: accepted
- Date: 2026-11-13

## Context

On 2026-11-10 PR #105 and PR #106 merged two minutes apart.
Each passed its checks against an older `main`. Together they broke CI on `main`.
The `main` ruleset had `strict_required_status_checks_policy = false`, set as a CI cost guard (CHB-56).
The owner decided on 2026-11-11: a branch is rebased on the latest `main` before its tests count, and no branch that misses commits from `main` merges.

## Decision

- The `main` ruleset sets `strict_required_status_checks_policy = true` (`infra/github/ruleset.tf`).
  `infra/github/tests/guards.tftest.hcl` fails the plan if it goes back to false.
- `factory/pr_updates.py` rebases every agent PR that is behind `main` and mergeable, approved or not.
  The rebase dismisses an approval (`dismiss_stale_reviews_on_push`). The owner approves the new head.
- For a Dependabot PR behind `main`, the agent App comments `@dependabot rebase`.
  Only Dependabot can push to its branches (`dependabot-branches` ruleset, ADR-ebe6).
- The agent does not skip the rebase to save CI minutes. CHB-56 keeps the other cost guards (path filters, cancelled superseded runs).

## Consequences

- Each merge to `main` makes every other open PR rebase and run its required checks again.
  With N open PRs, a merge costs up to N-1 check runs. The cost grows with the number of open PRs, so keep few PRs open.
- Each rebase of an approved PR needs a new owner approval. A PR waits for it while `main` moves.
- An owner approval and an auto-merge race with the next merge to `main`: the later PR is rebased and waits again.
- Unverified: that Dependabot answers a `@dependabot rebase` comment made by the agent App. If it does not, the Dependabot PR stays behind and the owner comments by hand.
- Owner step: apply `infra/github` after the merge (`make infra-plan`, then `make infra-apply`). Until then the old rule is in force.
