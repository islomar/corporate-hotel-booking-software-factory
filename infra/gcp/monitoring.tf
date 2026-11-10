# Uptime check, SLO and alert policies for the Cloud Run service (ADR-c17d, CHB-17).
# Both alert policies email the owner. The address comes from var.alert_email.

locals {
  service_host = trimprefix(google_cloud_run_v2_service.chb.uri, "https://")
  run_requests = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${google_cloud_run_v2_service.chb.name}\""
}

resource "google_monitoring_notification_channel" "email" {
  display_name = "chb owner email"
  type         = "email"
  labels = {
    email_address = var.alert_email
  }
}

resource "google_monitoring_uptime_check_config" "health" {
  display_name = "chb /api/health"
  period       = "300s"
  # min_instance_count is 0: a cold start must not count as a failure.
  timeout = "30s"
  # Each region holds at least one checker location. The API needs 3 locations or more.
  selected_regions = ["EUROPE", "USA", "ASIA_PACIFIC"]

  monitored_resource {
    type = "uptime_url"
    labels = {
      project_id = var.project_id
      host       = local.service_host
    }
  }

  http_check {
    path         = "/api/health"
    port         = 443
    use_ssl      = true
    validate_ssl = true
  }
}

# Fires when more than one checker location fails for 5 minutes.
resource "google_monitoring_alert_policy" "uptime" {
  display_name          = "chb is down (uptime check)"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]

  conditions {
    display_name = "/api/health fails"
    condition_threshold {
      filter          = "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND metric.label.check_id=\"${google_monitoring_uptime_check_config.health.uptime_check_id}\""
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      duration        = "300s"

      aggregations {
        alignment_period     = "1200s"
        per_series_aligner   = "ALIGN_NEXT_OLDER"
        cross_series_reducer = "REDUCE_COUNT_FALSE"
        group_by_fields      = ["resource.label.project_id", "resource.label.host"]
      }

      trigger {
        count = 1
      }
    }
  }

  documentation {
    content   = "GET https://${local.service_host}/api/health fails from more than one checker location. Check Cloud Run logs for the `chb` service."
    mime_type = "text/markdown"
  }
}

resource "google_monitoring_custom_service" "chb" {
  service_id   = "chb"
  display_name = "chb (Cloud Run)"

  telemetry {
    resource_name = "//run.googleapis.com/projects/${var.project_id}/locations/${var.region}/services/${google_cloud_run_v2_service.chb.name}"
  }
}

# Availability: requests that are not 5xx, over all requests.
# The uptime check's requests count too: request_count has no path label.
resource "google_monitoring_slo" "availability" {
  service             = google_monitoring_custom_service.chb.service_id
  slo_id              = "availability"
  display_name        = "99.5% of requests are not 5xx, rolling 28 days"
  goal                = 0.995
  rolling_period_days = 28

  request_based_sli {
    good_total_ratio {
      total_service_filter = local.run_requests
      bad_service_filter   = "${local.run_requests} AND metric.label.response_code_class=\"5xx\""
    }
  }
}

# 10x burn over 1 hour spends 28 days of error budget in under 3 days.
resource "google_monitoring_alert_policy" "slo_burn" {
  display_name          = "chb burns its error budget fast"
  combiner              = "OR"
  notification_channels = [google_monitoring_notification_channel.email.id]

  conditions {
    display_name = "Burn rate above 10x over 1 hour"
    condition_threshold {
      filter          = "select_slo_burn_rate(\"${google_monitoring_slo.availability.name}\", \"3600s\")"
      comparison      = "COMPARISON_GT"
      threshold_value = 10
      duration        = "0s"
    }
  }

  documentation {
    content   = "More than 10x the allowed rate of 5xx responses over the last hour (SLO: 99.5% over 28 days). Check Cloud Run logs and recent deploys."
    mime_type = "text/markdown"
  }
}
