resource "google_artifact_registry_repository" "chb" {
  repository_id = "chb"
  location      = var.region
  format        = "DOCKER"
  description   = "CHB app images"

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      older_than = "604800s"
      tag_state  = "ANY"
    }
  }

  # Five images cover rollbacks and stay under the 0.5 GB free tier (about 47 MB each). CHB-79.
  cleanup_policies {
    id     = "keep-last-5"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}
