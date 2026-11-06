# Deployment environments. Each one admits jobs from the main branch only (CHB-87).
#
# A same-repository branch can add a `pull_request` workflow, and that branch copy runs with
# repository secrets. Environment secrets limited to main never reach it: a `pull_request` job
# runs on refs/pull/N/merge. `pull_request_target` and `workflow_run` jobs run on the default
# branch, so review.yml and deploy.yml are admitted.
#
# Secret values never go into Terraform. The owner sets environment secrets by hand.

locals {
  environments = ["production", "reviewer"]
}

# Created by hand for deploy.yml (no protection rules, any branch).
import {
  to = github_repository_environment.this["production"]
  id = "${local.repository}:production"
}

resource "github_repository_environment" "this" {
  for_each    = toset(local.environments)
  repository  = github_repository.this.name
  environment = each.value

  # GitHub's current values for production. Admins (the owner) can still bypass.
  can_admins_bypass   = true
  prevent_self_review = false

  deployment_branch_policy {
    protected_branches     = false
    custom_branch_policies = true
  }
}

resource "github_repository_environment_deployment_policy" "main" {
  for_each       = github_repository_environment.this
  repository     = github_repository.this.name
  environment    = each.value.environment
  branch_pattern = "main"
}
