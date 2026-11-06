# Google grants the default compute service account roles/editor on the project.
# Nothing here uses it: the Compute Engine API is off, Cloud Run runs as chb-runtime,
# and the audit logs showed no activity by it (checked 2026-11-06). CHB-78.
#
# google_project_default_service_accounts (DEPRIVILEGE) found no accounts because it
# looks them up through the Compute Engine API, which is off. This removes the binding.
resource "google_project_iam_member_remove" "default_compute_editor" {
  project = var.project_id
  role    = "roles/editor"
  member  = "serviceAccount:${var.project_number}-compute@developer.gserviceaccount.com"
}
