<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-0faf: Short-lived branches, one ticket per pull request](#adr-0faf-short-lived-branches-one-ticket-per-pull-request)
  - [Context](#context)
  - [Decision](#decision)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-0faf: Short-lived branches, one ticket per pull request

- Status: accepted
- Date: 2026-11-05
- Amended 2026-11-13 (CHB-181): the Admin repository role bypasses the `main` ruleset, so the rule "applies to admins" no longer holds. See the CHB-119 amendment in ADR-d7b7 and `infra/github/ruleset.tf`.

## Context

eferro's practice is trunk-based development. The 540 software factory works through pull requests.
A human approves every pull request for now.

## Decision

- One Jira ticket, one branch, one pull request. Branch name: `CHB-<n>-<slug>`.
- A branch lives less than a day. Squash merge into `main`.
- `main` is protected: required checks, required approval, no force push, rules apply to admins.
- No push to `main` except the initial bootstrap.

## Consequences

- Close to trunk-based development, with a review point.
- Auto-merge comes later, only for low-risk tickets that pass every check and reach the review score threshold.
