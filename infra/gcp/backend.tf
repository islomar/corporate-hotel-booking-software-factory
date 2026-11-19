# State backend. Kept out of versions.tf, which has no code owner (CHB-207).
terraform {
  backend "gcs" {
    bucket = "chb-factory-a92932-tfstate"
    prefix = "gcp"
  }
}
