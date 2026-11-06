resource "google_artifact_registry_repository" "chb" {
  repository_id = "chb"
  location      = var.region
  format        = "DOCKER"
  description   = "CHB app images"

  cleanup_policy_dry_run = false

  # KEEP wins over DELETE: every image beyond the newest 5 is deleted once it is an hour old.
  # A 7-day age limit kept every image of a young project. CHB-79.
  cleanup_policies {
    id     = "delete-older"
    action = "DELETE"
    condition {
      older_than = "3600s"
      tag_state  = "ANY"
    }
  }

  # Five images cover rollbacks and stay under the 0.5 GB free tier (about 50 MB each).
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
