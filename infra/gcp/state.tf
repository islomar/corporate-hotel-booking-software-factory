# Created by hand before Terraform existed (CHB-10). Holds this configuration's state.
resource "google_storage_bucket" "tfstate" {
  name                        = "chb-factory-a92932-tfstate"
  location                    = "EUROPE-WEST1"
  storage_class               = "STANDARD"
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  soft_delete_policy {
    retention_duration_seconds = 604800
  }

  lifecycle {
    prevent_destroy = true
  }
}
