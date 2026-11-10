# Additive IAM only (*_iam_member). Never manage project IAM authoritatively:
# that would remove the owner and the Google service agents.

resource "google_service_account" "deployer" {
  account_id   = "github-deployer"
  display_name = "GitHub Actions deployer"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "runtime" {
  account_id   = "chb-runtime"
  display_name = "CHB Cloud Run runtime"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_project_iam_member" "deployer_run_developer" {
  project = var.project_id
  role    = "roles/run.developer"
  member  = google_service_account.deployer.member
}

resource "google_artifact_registry_repository_iam_member" "deployer_writer" {
  location   = var.region
  repository = google_artifact_registry_repository.chb.id
  role       = "roles/artifactregistry.writer"
  member     = google_service_account.deployer.member
}

resource "google_service_account_iam_member" "deployer_acts_as_runtime" {
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = google_service_account.deployer.member
}

# GitHub Actions on this repository's main branch log in as the deployer. No keys.
resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_condition                = "assertion.repository=='${var.github_repository}' && assertion.ref=='${var.deployer_ref}'"
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

resource "google_service_account_iam_member" "github_main_impersonates_deployer" {
  service_account_id = google_service_account.deployer.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_repository}"
}

# Telemetry from the app to Google's OTLP endpoint, telemetry.googleapis.com (CHB-15, ADR-c17d).
# Roles from https://cloud.google.com/stackdriver/docs/instrumentation/migrate-to-otlp-endpoints.
# Logs need no role: Cloud Run collects stdout itself.
resource "google_project_iam_member" "runtime_traces_writer" {
  project = var.project_id
  role    = "roles/telemetry.tracesWriter"
  member  = google_service_account.runtime.member
}

resource "google_project_iam_member" "runtime_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = google_service_account.runtime.member
}

# The deploy workflow writes one deployment event per deploy as a Cloud Logging entry
# (scripts/deploy_event.py, CHB-74). logWriter only creates entries; it reads none.
resource "google_project_iam_member" "deployer_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = google_service_account.deployer.member
}
