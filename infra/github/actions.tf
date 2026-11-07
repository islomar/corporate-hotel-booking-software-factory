# GitHub Actions settings (CHB-110). Before, they were GitHub defaults set by nobody.

# Only GitHub-owned actions (actions/*, github/*) and the third-party actions below may run,
# and every action must be pinned to a full commit SHA. Our workflows already pin.
# A new third-party action needs a line here first, or its workflow fails at "Set up job".
import {
  to = github_actions_repository_permissions.this
  id = local.repository
}

resource "github_actions_repository_permissions" "this" {
  repository           = github_repository.this.name
  enabled              = true
  allowed_actions      = "selected"
  sha_pinning_required = true

  allowed_actions_config {
    github_owned_allowed = true
    verified_allowed     = false
    # Owner/repository only: the SHA is checked by sha_pinning_required, and Dependabot bumps it.
    patterns_allowed = [
      "astral-sh/setup-uv@*",
      "danielroe/provenance-action@*",
      "dependabot/fetch-metadata@*",
      "google-github-actions/auth@*",
      "google-github-actions/setup-gcloud@*",
    ]
  }
}

# The workflow GITHUB_TOKEN reads by default, and it cannot approve pull requests.
# Both are true today; managing them stops a silent change (CHB-86 relies on them).
import {
  to = github_workflow_repository_permissions.this
  id = local.repository
}

resource "github_workflow_repository_permissions" "this" {
  repository                       = github_repository.this.name
  default_workflow_permissions     = "read"
  can_approve_pull_request_reviews = false
}
