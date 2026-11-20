# Guards for the CI identities (CHB-81). No credentials, no network.
# Run from infra/gcp: terraform init -backend=false && terraform test
# The first run is the control: every guard passes. Each later run turns one guard off.

mock_provider "google" {
  # Computed values that other resources validate.
  mock_resource "google_service_account" {
    defaults = {
      name   = "projects/chb-factory-a92932/serviceAccounts/mock@chb-factory-a92932.iam.gserviceaccount.com"
      member = "serviceAccount:mock@chb-factory-a92932.iam.gserviceaccount.com"
    }
  }
  mock_resource "google_iam_workload_identity_pool" {
    defaults = {
      name = "projects/1027266991110/locations/global/workloadIdentityPools/mock"
    }
  }
}

variables {
  billing_account = "000000-000000-000000"
  alert_email     = "alerts@example.invalid"
}

# A mock provider cannot import. These are the targets of the import blocks (imports.tf).
override_resource { target = google_storage_bucket.tfstate }
override_resource { target = google_artifact_registry_repository.chb }
override_resource {
  target = google_service_account.deployer
  values = {
    name   = "projects/chb-factory-a92932/serviceAccounts/github-deployer@chb-factory-a92932.iam.gserviceaccount.com"
    member = "serviceAccount:github-deployer@chb-factory-a92932.iam.gserviceaccount.com"
  }
}
override_resource {
  target = google_service_account.runtime
  values = {
    name   = "projects/chb-factory-a92932/serviceAccounts/chb-runtime@chb-factory-a92932.iam.gserviceaccount.com"
    member = "serviceAccount:chb-runtime@chb-factory-a92932.iam.gserviceaccount.com"
  }
}
override_resource {
  target = google_iam_workload_identity_pool.github
  values = { name = "projects/1027266991110/locations/global/workloadIdentityPools/github" }
}
override_resource { target = google_iam_workload_identity_pool_provider.github }
# Known names, so all_guards_pass can check the planner's ID binding at plan time (CHB-220).
# Terraform creates these two (no import block), so their names are unknown at plan without override_during.
override_resource {
  target          = google_iam_workload_identity_pool.github_pr
  override_during = plan
  values          = { name = "projects/1027266991110/locations/global/workloadIdentityPools/github-pr" }
}
override_resource {
  target          = google_service_account.planner
  override_during = plan
  values = {
    name   = "projects/chb-factory-a92932/serviceAccounts/github-planner@chb-factory-a92932.iam.gserviceaccount.com"
    member = "serviceAccount:github-planner@chb-factory-a92932.iam.gserviceaccount.com"
  }
}
override_resource { target = google_project_iam_member.deployer_run_developer }
override_resource { target = google_artifact_registry_repository_iam_member.deployer_writer }
override_resource { target = google_service_account_iam_member.deployer_acts_as_runtime }
override_resource { target = google_service_account_iam_member.github_main_impersonates_deployer }
override_resource { target = google_billing_budget.monthly }
override_resource { target = google_cloud_run_v2_service.chb }

override_resource {
  target = google_project_service.enabled
}

run "all_guards_pass" {
  command = plan

  # CHB-220: both conditions and the new bindings name the repository ID, which a transfer is expected to keep (CHB-221 checks it).
  # The old name bindings stay until CHB-204 removes them.
  assert {
    condition     = google_iam_workload_identity_pool_provider.github.attribute_condition == "assertion.repository_id=='1447565814' && assertion.ref=='refs/heads/main'"
    error_message = "The deployer's condition must trust the repository ID and main only."
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.github_pr.attribute_condition == "assertion.repository_id=='1447565814' && assertion.event_name=='pull_request'"
    error_message = "The planner's condition must trust the repository ID and pull_request only."
  }
  assert {
    condition     = google_service_account_iam_member.github_main_impersonates_deployer_by_id.member == "principalSet://iam.googleapis.com/projects/1027266991110/locations/global/workloadIdentityPools/github/attribute.repository_id/1447565814"
    error_message = "The deployer's ID binding must trust the repository ID in the github pool."
  }
  assert {
    condition     = google_service_account_iam_member.github_main_impersonates_deployer_by_id.service_account_id == google_service_account.deployer.name
    error_message = "The deployer's ID binding must be on the deployer."
  }
  assert {
    condition     = google_service_account_iam_member.github_pr_impersonates_planner_by_id.member == "principalSet://iam.googleapis.com/projects/1027266991110/locations/global/workloadIdentityPools/github-pr/attribute.repository_id/1447565814"
    error_message = "The planner's ID binding must trust the repository ID in the planner pool."
  }
  assert {
    condition     = google_service_account_iam_member.github_pr_impersonates_planner_by_id.service_account_id == google_service_account.planner.name
    error_message = "The planner's ID binding must be on the planner."
  }
}

run "planner_with_editor_fails_the_plan" {
  command = plan

  variables {
    planner_project_roles = ["roles/viewer", "roles/editor"]
  }

  expect_failures = [terraform_data.guards]
}

run "planner_with_write_on_the_state_bucket_fails_the_plan" {
  command = plan

  variables {
    planner_state_roles = ["roles/storage.objectAdmin"]
  }

  expect_failures = [terraform_data.guards]
}

run "planner_with_service_usage_admin_fails_the_plan" {
  command = plan

  # serviceUsageConsumer is allowed (ADR-3c1f, amended 2026-11-15); the admin role can enable APIs.
  variables {
    planner_project_roles = ["roles/viewer", "roles/serviceusage.serviceUsageAdmin"]
  }

  expect_failures = [terraform_data.guards]
}

run "planner_with_fewer_roles_passes" {
  command = plan

  variables {
    planner_project_roles = ["roles/viewer"]
  }
}

run "repository_name_in_place_of_the_id_fails_the_plan" {
  command = plan

  # A name can be reused after a transfer or a rename; the numeric ID cannot (CHB-220).
  variables {
    github_repository_id = "islomar/corporate-hotel-booking-software-factory"
  }

  expect_failures = [var.github_repository_id]
}

run "another_repository_id_fails_the_plan" {
  command = plan

  # The guard pins the ID; an override of the variable must not change what Google trusts (CHB-220 review).
  variables {
    github_repository_id = "1"
  }

  expect_failures = [terraform_data.guards]
}

run "deployer_condition_moved_to_another_branch_fails_the_plan" {
  command = plan

  variables {
    deployer_ref = "refs/heads/develop"
  }

  expect_failures = [terraform_data.guards]
}

run "planner_condition_on_another_event_fails_the_plan" {
  command = plan

  variables {
    planner_event_name = "push"
  }

  expect_failures = [terraform_data.guards]
}

run "planner_provider_in_the_deployer_pool_fails_the_plan" {
  command = plan

  variables {
    planner_pool_id = "github"
  }

  expect_failures = [terraform_data.guards]
}
