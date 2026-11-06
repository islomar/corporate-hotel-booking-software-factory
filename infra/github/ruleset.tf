# Applies to everyone, the owner included: no bypass actors (ADR-d7b7).
resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = github_repository.this.name
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["~DEFAULT_BRANCH"]
      exclude = []
    }
  }

  rules {
    deletion         = true
    non_fast_forward = true

    pull_request {
      required_approving_review_count   = 1
      require_code_owner_review         = true
      dismiss_stale_reviews_on_push     = true
      require_last_push_approval        = true
      required_review_thread_resolution = true
      allowed_merge_methods             = ["squash"]
    }

    # Copilot code review is a second opinion only (CHB-106): it leaves comments, is not a
    # required check and does not count as an approval. Leave the repository setting "Allow
    # Copilot approvals to count toward merge requirements" off. GitHub requests the review
    # only if the PR author has Copilot code review; for App-authored PRs this may do nothing.
    # One review per PR, none on drafts, to save the owner's Copilot quota.
    copilot_code_review {
      review_on_push             = false
      review_draft_pull_requests = false
    }

    required_status_checks {
      # false: a PR need not be rebased on the latest main before merging (CI cost guard, CHB-56).
      strict_required_status_checks_policy = false

      dynamic "required_check" {
        for_each = local.required_checks
        content {
          context        = required_check.value
          integration_id = local.github_actions_app_id
        }
      }
    }
  }
}

# Only Dependabot writes to its branches. The required check `supply-chain` trusts commits
# whose metadata says Dependabot (ADR-ebe6); this ruleset stops anyone else, the agent App
# and the owner included, from pushing such commits to a Dependabot PR. To refresh a
# Dependabot PR, comment `@dependabot rebase`.
resource "github_repository_ruleset" "dependabot_branches" {
  name        = "dependabot-branches"
  repository  = github_repository.this.name
  target      = "branch"
  enforcement = "active"

  conditions {
    ref_name {
      include = ["refs/heads/dependabot/**/*"]
      exclude = []
    }
  }

  bypass_actors {
    actor_id    = local.dependabot_app_id
    actor_type  = "Integration"
    bypass_mode = "always"
  }

  # No `deletion`: deleting a branch cannot forge a commit, and blocking it would stop
  # delete_branch_on_merge from removing merged Dependabot branches.
  rules {
    creation = true
    update   = true
  }
}
