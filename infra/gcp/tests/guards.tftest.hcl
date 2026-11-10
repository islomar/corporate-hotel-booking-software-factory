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

run "planner_with_fewer_roles_passes" {
  command = plan

  variables {
    planner_project_roles = ["roles/viewer"]
  }
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
