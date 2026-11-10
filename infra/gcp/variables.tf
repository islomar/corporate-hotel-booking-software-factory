variable "project_id" {
  type    = string
  default = "chb-factory-a92932"
}

variable "project_number" {
  type    = string
  default = "1027266991110"
}

variable "region" {
  type    = string
  default = "europe-west1"
}

variable "github_repository" {
  type    = string
  default = "islomar/corporate-hotel-booking-software-factory"
}

variable "billing_account" {
  description = "Billing account ID. Pass with TF_VAR_billing_account; never commit it."
  type        = string
  sensitive   = true
}

variable "alert_email" {
  description = "Address that receives the alerts. Pass with TF_VAR_alert_email; never commit it."
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

# The next five variables exist so tests/guards.tftest.hcl can break each guard in guards.tf.
# Keep the defaults. Any other value fails the plan (CHB-81).
variable "planner_project_roles" {
  description = "Project roles of the read-only planner. guards.tf allows read-only roles only."
  type        = set(string)
  default     = ["roles/viewer", "roles/iam.securityReviewer"]
}

variable "planner_state_roles" {
  description = "Roles of the planner on the state bucket. guards.tf allows read-only roles only."
  type        = set(string)
  default     = ["roles/storage.objectViewer"]
}

variable "deployer_ref" {
  description = "The only ref the deployer trusts. guards.tf requires refs/heads/main (ADR-d7b7)."
  type        = string
  default     = "refs/heads/main"
}

variable "planner_event_name" {
  description = "The only workflow event the planner trusts. guards.tf requires pull_request."
  type        = string
  default     = "pull_request"
}

variable "planner_pool_id" {
  description = "Pool of the planner provider. guards.tf requires a pool other than the deployer's."
  type        = string
  default     = "github-pr"
}
