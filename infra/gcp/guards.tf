# Guards for the CI identities (CHB-81). Checked on every plan and by tests/guards.tftest.hcl.
# A failed guard stops `make infra-plan`. Fix the code, not the guard.

locals {
  # The only roles the planner may hold. All read-only. Widening this list needs an ADR and the owner.
  planner_role_allowlist = toset([
    "roles/viewer",
    "roles/iam.securityReviewer",
    "roles/storage.objectViewer",
  ])
  planner_roles = setunion(
    [for b in google_project_iam_member.planner : b.role],
    [for b in google_storage_bucket_iam_member.planner_state : b.role],
  )
  # The deployer logs in from main only. Never widen this (ADR-d7b7).
  deployer_attribute_condition = "assertion.repository=='${var.github_repository}' && assertion.ref=='refs/heads/main'"
  planner_attribute_condition  = "assertion.repository=='${var.github_repository}' && assertion.event_name=='pull_request'"
}

resource "terraform_data" "guards" {
  lifecycle {
    precondition {
      condition     = length(setsubtract(local.planner_roles, local.planner_role_allowlist)) == 0
      error_message = "The planner holds roles outside the read-only allowlist: ${join(", ", setsubtract(local.planner_roles, local.planner_role_allowlist))}. It must only read (CHB-81, ADR-3c1f)."
    }
    precondition {
      condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == local.deployer_attribute_condition
      error_message = "The deployer's Workload Identity condition changed. It must be exactly: ${local.deployer_attribute_condition} (ADR-d7b7, CHB-81)."
    }
    precondition {
      condition     = google_iam_workload_identity_pool_provider.github_pr.attribute_condition == local.planner_attribute_condition
      error_message = "The planner's Workload Identity condition changed. It must be exactly: ${local.planner_attribute_condition} (CHB-81)."
    }
    precondition {
      condition     = google_iam_workload_identity_pool_provider.github_pr.workload_identity_pool_id != google_iam_workload_identity_pool_provider.github.workload_identity_pool_id
      error_message = "The planner's provider must live in its own pool. The deployer binding trusts the whole `github` pool (CHB-81)."
    }
  }
}
