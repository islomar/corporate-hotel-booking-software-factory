# The deploy workflow changes only the image (gcloud run deploy --image).
# Terraform owns every other setting and ignores the image and the client fields.
resource "google_cloud_run_v2_service" "chb" {
  name                 = "chb"
  location             = var.region
  ingress              = "INGRESS_TRAFFIC_ALL"
  invoker_iam_disabled = true # public app: login happens inside the app (ADR-50e4)
  deletion_protection  = true

  scaling {
    max_instance_count = 3
  }

  template {
    service_account                  = google_service_account.runtime.email
    max_instance_request_concurrency = 80
    timeout                          = "300s"

    # In-memory repositories: one instance keeps one consistent view of the data (ADR-d5e6).
    scaling {
      min_instance_count = 0
      max_instance_count = 1
    }

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/chb/app:bootstrap"

      # Telemetry goes to Google's OTLP endpoint with the runtime identity (ADR-c17d, CHB-15).
      env {
        name  = "OTEL_EXPORTER_OTLP_ENDPOINT"
        value = "https://telemetry.googleapis.com"
      }
      env {
        name  = "GOOGLE_CLOUD_PROJECT"
        value = var.project_id
      }

      ports {
        name           = "http1"
        container_port = 8080
      }

      resources {
        cpu_idle          = true
        startup_cpu_boost = true
        limits = {
          cpu    = "1000m"
          memory = "512Mi"
        }
      }

      startup_probe {
        failure_threshold = 1
        period_seconds    = 240
        timeout_seconds   = 240
        tcp_socket {
          port = 8080
        }
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  lifecycle {
    prevent_destroy = true
    ignore_changes = [
      client,
      client_version,
      template[0].containers[0].image,
    ]
  }
}
