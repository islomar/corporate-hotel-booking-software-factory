# Google grants the default compute service account roles/editor on the project.
# Nothing here uses it: the Compute Engine API is off, Cloud Run runs as chb-runtime,
# and the audit logs showed no activity by it (checked 2026-11-06). CHB-78.
resource "google_project_default_service_accounts" "deprivileged" {
  project        = var.project_id
  action         = "DEPRIVILEGE"
  restore_policy = "REVERT"
}
