<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Security policy](#security-policy)
  - [Report a vulnerability](#report-a-vulnerability)
  - [Scope](#scope)
  - [What is in place](#what-is-in-place)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Security policy

This repository is a learning experiment: a demo hotel booking app built by an AI software factory.
The production app holds no real bookings and no personal data.
Security reports are still welcome, and they help the factory improve.

## Report a vulnerability

Report it privately through GitHub: open the **Security** tab of this repository and choose **Report a vulnerability**.
Do not open a public issue or pull request for a vulnerability.

Include what you found, where, and how to reproduce it.
The maintainer answers within 7 days. This project pays no bug bounty.

## Scope

- The app in `backend/` and `frontend/`, and its deployment on Google Cloud.
- The factory: `factory/`, `.github/workflows/`, `infra/` and the agent's GitHub App and Jira access.
- A leaked secret in this repository or its history.

Out of scope: denial of service, automated scanner output without a working impact, and the third-party services themselves (GitHub, Google Cloud, Atlassian, Anthropic, 1Password).

## What is in place

- Secret scanning and push protection are on (`infra/github/repository.tf`). GitHub refuses a push that contains a known secret format.
- Dependabot alerts are on. Dependency updates go through the supply-chain check before they merge.
- Secrets for workflows live in environments that only the `main` branch can use (ADR-d7b7).
- The agent acts through its own GitHub App, bound by the `main` ruleset, and its own Jira account (ADR-d7b7).
