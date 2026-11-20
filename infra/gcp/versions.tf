# Version pins only. Dependabot updates this file, and it has no code owner: the required
# check `supply-chain` decides (ADR-ebe6, CHB-207). Configuration that is not a version pin
# lives in code-owned files: the state backend in backend.tf, the provider in providers.tf.
terraform {
  required_version = "~> 1.17.0"

  required_providers {
    google = {
      source = "hashicorp/google"
      # 8.10.0 was younger than the 7-day release-age window on 2026-11-06 (ADR-ebe6).
      version = "~> 8.11.0"
    }
  }
}
