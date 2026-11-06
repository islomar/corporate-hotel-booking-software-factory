<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-aae8: Infrastructure as Code with Terraform](#adr-aae8-infrastructure-as-code-with-terraform)
  - [Context](#context)
  - [Decision](#decision)
  - [Alternatives rejected](#alternatives-rejected)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-aae8: Infrastructure as Code with Terraform

- Status: accepted
- Date: 2026-11-06

## Context

The GCP project, registry, service accounts, Workload Identity Federation, budget and Cloud Run service were created by hand with gcloud.
GitHub settings (rulesets, variables, environments) were set by hand too.
Hand-made infrastructure cannot be reviewed, reproduced or rolled back.

## Decision

- Terraform manages all infrastructure in `infra/`: Google Cloud, GitHub repository settings and monitoring.
- State lives in a versioned GCS bucket. The bucket is the one resource created by hand (bootstrap).
- Resources created by hand come in through `import` blocks.
- The deploy workflow keeps changing the Cloud Run image. Terraform ignores the image and client fields of the service.
- Rollout order:
  1. The user runs `terraform apply` locally. CI runs `terraform plan` on pull requests.
  2. CI applies only after the agent has its own GitHub identity, the `main` ruleset exists, CODEOWNERS covers `infra/`, and a protected environment gates the apply.
     Applying changes IAM, so it needs a stronger identity than the deploy service account.

## Alternatives rejected

- OpenTofu: compatible, open licence, state encryption. Terraform's licence does not restrict this project and has more examples. Switching later is cheap.
- Pulumi with Python: same language as the backend, but imperative code gives a plan that is harder to review, and agents write it less reliably.

## Consequences

- Infrastructure changes go through pull requests with a plan diff.
- The billing account ID is not committed. It is passed as a variable.
