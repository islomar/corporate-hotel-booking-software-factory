# Sign-in with Identity Platform (ADR-50e4, CHB-20).
# Email and password is managed here. The Google provider is not: its resource needs the
# OAuth client secret, and a secret in Terraform lands in the state. The owner adds it in
# the console (CHB-20, "Needs human: owner steps").

locals {
  # Not in local.services: that set also drives the import blocks, and these were never enabled.
  auth_services = toset([
    "apikeys.googleapis.com",
    "identitytoolkit.googleapis.com",
    "securetoken.googleapis.com",
  ])
  firebase_auth_domain = "${var.project_id}.firebaseapp.com"
  app_host             = "chb-${var.project_number}.${var.region}.run.app"
}

resource "google_project_service" "auth" {
  for_each = local.auth_services
  service  = each.key

  disable_on_destroy         = false
  disable_dependent_services = false
}

# Created once and never deleted: Google cannot turn Identity Platform off again.
resource "google_identity_platform_config" "default" {
  sign_in {
    allow_duplicate_emails = false

    email {
      enabled           = true
      password_required = true
    }

    # Charged per SMS: stays off (ADR-50e4).
    phone_number {
      enabled = false
    }
  }

  # SMS MFA is charged per SMS: stays off (ADR-50e4).
  mfa {
    state = "DISABLED"
  }

  # localhost lets `make dev` sign in against this project: there is no separate dev project.
  authorized_domains = [
    "localhost",
    local.firebase_auth_domain,
    local.app_host,
  ]

  depends_on = [google_project_service.auth]
}

# The browser key of the Firebase JS SDK. Public by design: it identifies the project,
# it grants nothing. The restrictions keep it to the sign-in APIs and our pages.
resource "google_apikeys_key" "browser" {
  name         = "chb-browser"
  display_name = "CHB browser sign-in (Terraform)"

  restrictions {
    api_targets {
      service = "identitytoolkit.googleapis.com"
    }
    api_targets {
      service = "securetoken.googleapis.com"
    }

    browser_key_restrictions {
      allowed_referrers = [
        "https://${local.app_host}/*",
        "https://${local.firebase_auth_domain}/*",
        "http://localhost:5173/*",
      ]
    }
  }

  depends_on = [google_project_service.auth]
}

output "firebase_web_config" {
  description = "Public web config of the Firebase JS SDK. Cloud Run serves it at /auth-config.json."
  value = {
    apiKey     = google_apikeys_key.browser.key_string
    authDomain = local.firebase_auth_domain
  }
  sensitive = true
}
