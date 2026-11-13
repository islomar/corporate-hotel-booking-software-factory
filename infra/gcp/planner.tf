# Read-only identity for `terraform plan` in pull requests (CHB-81, ADR-3c1f).
# CHB-13 uses it. It cannot change anything: guards.tf fails the plan when it holds a role outside
# a read-only allowlist.

# A separate pool, not a second provider in the `github` pool. The deployer binding
# (identities.tf) trusts `attribute.repository` of the whole `github` pool. A provider with the
# same mapping in that pool would let pull_request runs impersonate the deployer.
resource "google_iam_workload_identity_pool" "github_pr" {
  workload_identity_pool_id = var.planner_pool_id
  display_name              = "GitHub Actions pull requests"

  lifecycle {
    prevent_destroy = true
  }
}

# Only `pull_request` runs of this repository. Runs from a fork get no OIDC token from GitHub.
resource "google_iam_workload_identity_pool_provider" "github_pr" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github_pr.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-pr"
  attribute_condition                = "assertion.repository=='${var.github_repository}' && assertion.event_name=='${var.planner_event_name}'"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "planner" {
  account_id   = "github-planner"
  display_name = "GitHub Actions planner (read-only)"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account_iam_member" "github_pr_impersonates_planner" {
  service_account_id = google_service_account.planner.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github_pr.name}/attribute.repository/${var.github_repository}"
}

locals {
  # Every role the planner holds. Add one only if it is in `local.planner_role_allowlist` (guards.tf).
  # viewer reads resources. securityReviewer reads IAM policies, which the refresh of the
  # `*_iam_member` resources needs.
  planner_project_roles = var.planner_project_roles
  # Reads the state file. `plan -lock=false` takes no lock, so it needs no write access.
  planner_state_roles = var.planner_state_roles
}

resource "google_project_iam_member" "planner" {
  for_each = local.planner_project_roles
  project  = var.project_id
  role     = each.key
  member   = google_service_account.planner.member
}

resource "google_storage_bucket_iam_member" "planner_state" {
  for_each = local.planner_state_roles
  bucket   = google_storage_bucket.tfstate.name
  role     = each.key
  member   = google_service_account.planner.member
}
