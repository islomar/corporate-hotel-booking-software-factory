# State backend (CHB-216). Before it, the state was a local file on the human's laptop.
# Migration steps: infra/README.md, "Move the github state to the bucket".
terraform {
  backend "gcs" {
    bucket = "chb-factory-a92932-tfstate"
    prefix = "github"
  }
}
