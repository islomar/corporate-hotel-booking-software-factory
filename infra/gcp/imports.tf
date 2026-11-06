# Resources created by hand on 2026-11-05 and 2026-11-06 (CHB-11).
locals {
  p        = var.project_id
  deployer = "github-deployer@${var.project_id}.iam.gserviceaccount.com"
  runtime  = "chb-runtime@${var.project_id}.iam.gserviceaccount.com"
  repo     = "projects/${var.project_id}/locations/${var.region}/repositories/chb"
  pool     = "projects/${var.project_id}/locations/global/workloadIdentityPools/github"
  services = toset([
    "artifactregistry.googleapis.com",
    "billingbudgets.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtrace.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "sts.googleapis.com",
    # Google's OTLP endpoint for traces and metrics (CHB-15). Already enabled on 2026-11-06.
    "telemetry.googleapis.com",
  ])
}

import {
  for_each = local.services
  to       = google_project_service.enabled[each.key]
  id       = "${local.p}/${each.key}"
}

import {
  to = google_storage_bucket.tfstate
  id = "chb-factory-a92932-tfstate"
}

import {
  to = google_artifact_registry_repository.chb
  id = local.repo
}

import {
  to = google_service_account.deployer
  id = "projects/${local.p}/serviceAccounts/${local.deployer}"
}

import {
  to = google_service_account.runtime
  id = "projects/${local.p}/serviceAccounts/${local.runtime}"
}

import {
  to = google_iam_workload_identity_pool.github
  id = local.pool
}

import {
  to = google_iam_workload_identity_pool_provider.github
  id = "${local.pool}/providers/github"
}

import {
  to = google_project_iam_member.deployer_run_developer
  id = "${local.p} roles/run.developer serviceAccount:${local.deployer}"
}

import {
  to = google_artifact_registry_repository_iam_member.deployer_writer
  id = "${local.repo} roles/artifactregistry.writer serviceAccount:${local.deployer}"
}

import {
  to = google_service_account_iam_member.deployer_acts_as_runtime
  id = "projects/${local.p}/serviceAccounts/${local.runtime} roles/iam.serviceAccountUser serviceAccount:${local.deployer}"
}

import {
  to = google_service_account_iam_member.github_main_impersonates_deployer
  id = "projects/${local.p}/serviceAccounts/${local.deployer} roles/iam.workloadIdentityUser principalSet://iam.googleapis.com/projects/${var.project_number}/locations/global/workloadIdentityPools/github/attribute.repository/${var.github_repository}"
}

import {
  to = google_billing_budget.monthly
  id = "billingAccounts/${var.billing_account}/budgets/dfd5ce0c-919e-4c5e-aaf8-cd57546de460"
}

import {
  to = google_cloud_run_v2_service.chb
  id = "projects/${local.p}/locations/${var.region}/services/chb"
}
