resource "google_project_service" "enabled" {
  for_each = local.services
  service  = each.key

  disable_on_destroy         = false
  disable_dependent_services = false
}
