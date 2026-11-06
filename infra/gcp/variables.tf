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
