# Provider configuration. Kept out of versions.tf, which has no code owner (CHB-207).
provider "google" {
  project = var.project_id
  region  = var.region
  # Budgets API calls with user credentials need a quota project.
  user_project_override = true
  billing_project       = var.project_id
}
