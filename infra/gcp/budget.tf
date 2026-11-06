# Warns at 50, 90 and 100 percent. A budget never stops spending.
resource "google_billing_budget" "monthly" {
  billing_account = var.billing_account
  display_name    = "chb-factory monthly 5 EUR"

  amount {
    specified_amount {
      currency_code = "EUR"
      units         = "5"
    }
  }

  budget_filter {
    projects               = ["projects/${var.project_number}"]
    calendar_period        = "MONTH"
    credit_types_treatment = "INCLUDE_ALL_CREDITS"
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }
}
