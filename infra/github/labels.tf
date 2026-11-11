# Labels that .github/workflows/dependabot-auto-merge.yml sets (CHB-110).
# Created by hand before; a rename or a deletion would make `gh pr edit --add-label` fail,
# and the workflow only warns. Colors and descriptions match the live labels.
locals {
  labels = {
    "dependencies"             = { color = "0366d6", description = "Pull requests that update a dependency file" }
    "dependencies:auto-merge"  = { color = "0e8a16", description = "Safe Dependabot update, merged automatically after required checks" }
    "dependencies:safe"        = { color = "c2e0c6", description = "Safe Dependabot update; auto-merge is off, a person merges" }
    "dependencies:needs-human" = { color = "d93f0b", description = "Dependabot update that needs a human review" }
  }
}

import {
  for_each = local.labels
  to       = github_issue_label.this[each.key]
  id       = "${local.repository}:${each.key}"
}

resource "github_issue_label" "this" {
  for_each    = local.labels
  repository  = github_repository.this.name
  name        = each.key
  color       = each.value.color
  description = each.value.description
}

# Set by .github/workflows/pr-updates.yml on an agent PR that conflicts with main (CHB-172).
# GitHub runs no `pull_request` workflow on such a PR; the SessionStart sweep lists it.
# New label: nothing to import.
resource "github_issue_label" "needs_rebase" {
  repository  = github_repository.this.name
  name        = "needs-rebase"
  color       = "e99695"
  description = "Agent PR with a merge conflict with main; the agent resolves it in a session"
}

# CHB-<n> in commits, pull requests and comments links to the Jira ticket (CHB-110).
# The other direction, Jira showing commits, is CHB-130.
resource "github_repository_autolink_reference" "jira" {
  repository          = github_repository.this.name
  key_prefix          = "CHB-"
  target_url_template = "https://islomar.atlassian.net/browse/CHB-<num>"
  is_alphanumeric     = false
}
