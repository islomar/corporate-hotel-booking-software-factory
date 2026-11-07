<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-d7b7: A GitHub App is the agent's identity](#adr-d7b7-a-github-app-is-the-agents-identity)
  - [Context](#context)
  - [Decision](#decision)
  - [Alternatives rejected](#alternatives-rejected)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-d7b7: A GitHub App is the agent's identity

- Status: accepted
- Date: 2026-11-06

## Context

The agent used `gh` logged in as the repository owner, who is admin.
GitHub does not let a PR author approve their own PR, so the owner could not approve agent PRs.
An admin token bypasses rulesets and CODEOWNERS.
Events created with the default `GITHUB_TOKEN` start no new workflow runs, so bot merges would skip CI and deploy on `main`.

## Decision

- A GitHub App, installed only on this repository, acts for the agent and for automation workflows.
- Permissions: contents, pull requests and workflows read and write; checks, actions and metadata read. No administration.
- Tokens are short-lived and minted per run. The private key stays in 1Password and in a GitHub Actions secret.
- The `main` ruleset applies to everyone except the Admin repository role: pull request, required checks `backend`, `frontend`, `e2e`, code-owner review on harness paths.
- Amended 2026-11-07 (CHB-119): the Admin role bypasses the `main` ruleset, so the owner can push directly to `main`.
  Only the owner holds that role. The agent App has no administration permission and stays bound by the ruleset.

## Alternatives rejected

- A second GitHub account: a long-lived token, its own email and 2FA.
- The owner's identity: protections become decoration.

## Consequences

- The owner approves agent PRs as code owner.
- Merges done by the App trigger CI and deploy.
- The owner's direct pushes to `main` skip the pull request, the required checks and code-owner review.
  CI and deploy still run after the push, so an untested commit can reach production.
