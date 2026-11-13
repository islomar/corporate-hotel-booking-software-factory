locals {
  repository = "corporate-hotel-booking-software-factory"
  # GitHub Actions. Pinning checks to it stops any other app from satisfying them.
  github_actions_app_id = 15368
  # Dependabot, from `gh api /apps/dependabot --jq .id`.
  dependabot_app_id = 29110
  required_checks   = ["backend", "frontend", "e2e", "supply-chain"]

  variables = {
    APP_URL          = "https://chb-1027266991110.europe-west1.run.app"
    GCP_DEPLOYER_SA  = "github-deployer@chb-factory-a92932.iam.gserviceaccount.com"
    GCP_PROJECT      = "chb-factory-a92932"
    GCP_REGION       = "europe-west1"
    GCP_WIF_PROVIDER = "projects/1027266991110/locations/global/workloadIdentityPools/github/providers/github"
  }
}

import {
  to = github_repository.this
  id = local.repository
}

resource "github_repository" "this" {
  name         = local.repository
  description  = "Codurance Corporate Hotel Booking kata built by an AI-agent software factory: outside-in TDD, trunk-based development, continuous delivery. Practices from eferro and the 540 talk."
  homepage_url = local.variables.APP_URL
  visibility   = "public"

  # GitHub allows 20 topics at most.
  topics = [
    "software-factory",
    "ai-agents",
    "agentic-coding",
    "claude-code",
    "ai-assisted-development",
    "kata",
    "code-kata",
    "corporate-hotel-booking",
    "codurance",
    "tdd",
    "outside-in-tdd",
    "extreme-programming",
    "continuous-delivery",
    "trunk-based-development",
    "fastapi",
    "react",
    "playwright",
    "google-cloud-run",
    "terraform",
    "opentelemetry",
  ]

  # Work is tracked in Jira (CHB). Docs live in the repository.
  has_issues   = false
  has_projects = false
  has_wiki     = false

  # One ticket, one squash commit (ADR-0faf).
  allow_squash_merge          = true
  allow_merge_commit          = false
  allow_rebase_merge          = false
  allow_auto_merge            = true
  allow_update_branch         = true
  delete_branch_on_merge      = true
  squash_merge_commit_title   = "PR_TITLE"
  squash_merge_commit_message = "PR_BODY"

  security_and_analysis {
    secret_scanning {
      status = "enabled"
    }
    secret_scanning_push_protection {
      status = "enabled"
    }
  }

  archive_on_destroy = true

  lifecycle {
    prevent_destroy = true
  }
}

# Dependabot alerts. Removing the deprecated vulnerability_alerts field from
# github_repository leaves the setting alone: that field is Optional+Computed.
import {
  to = github_repository_vulnerability_alerts.this
  id = local.repository
}

resource "github_repository_vulnerability_alerts" "this" {
  repository = github_repository.this.name
  enabled    = true

  # Destroying this resource disables the alerts on GitHub.
  lifecycle {
    prevent_destroy = true
  }
}

import {
  for_each = local.variables
  to       = github_actions_variable.this[each.key]
  id       = "${local.repository}:${each.key}"
}

resource "github_actions_variable" "this" {
  for_each      = local.variables
  repository    = github_repository.this.name
  variable_name = each.key
  value         = each.value
}

# Dependabot security updates (CHB-95): a PR for each Dependabot alert with a fix.
# They skip the dependabot.yml cooldown. The supply-chain check still decides the merge:
# patch, or minor at 1.0 or above, merges unattended; anything else waits for the owner.
# Needs the alerts above.
import {
  to = github_repository_dependabot_security_updates.this
  id = local.repository
}

resource "github_repository_dependabot_security_updates" "this" {
  repository = github_repository.this.name
  enabled    = true

  depends_on = [github_repository_vulnerability_alerts.this]
}
