terraform {
  required_version = "~> 1.17.0"

  backend "gcs" {
    bucket = "chb-factory-a92932-tfstate"
    prefix = "gcp"
  }

  required_providers {
    google = {
      source = "hashicorp/google"
      # 8.10.0 was younger than the 7-day release-age window on 2026-11-06 (ADR-ebe6).
      version = "~> 8.10.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
  # Budgets API calls with user credentials need a quota project.
  user_project_override = true
  billing_project       = var.project_id
}
