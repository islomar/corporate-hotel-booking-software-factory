# Applies to everyone except the Admin role, which only the owner holds (ADR-d7b7, CHB-119).
# The agent App has no administration permission, so the ruleset still binds it.
resource "github_repository_ruleset" "main" {
  name        = "main"
  repository  = github_repository.this.name
  target      = "branch"
  enforcement = "active"

  bypass_actors {
    actor_id    = 5 # built-in Admin repository role
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

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

    # No copilot_code_review rule (CHB-106, discarded): GitHub requests it only when the PR
    # author has a Copilot seat, and every PR here is opened by the agent App, so it never fired.
    # The owner can still request a Copilot review by hand from the PR's Reviewers menu.

    required_status_checks {
      # true: a PR merges only when its head contains the latest main, so its checks ran on what
      # main will be (CHB-177). Costs one CI run per PR for every merge to main; ADR-2ba2.
      strict_required_status_checks_policy = true

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

# CHB-173: a tag named `main` passes `branches: [main]` in deploy.yml's workflow_run filter,
# because GitHub matches the short name of the triggering ref. Only the Admin role (the owner)
# may create, move or delete such a tag. deploy.yml's gate job refuses a commit off main anyway;
# this ruleset stops the tag from being pushed at all.
resource "github_repository_ruleset" "branch_named_tags" {
  name        = "branch-named-tags"
  repository  = github_repository.this.name
  target      = "tag"
  enforcement = "active"

  bypass_actors {
    actor_id    = 5 # built-in Admin repository role
    actor_type  = "RepositoryRole"
    bypass_mode = "always"
  }

  conditions {
    ref_name {
      include = ["refs/tags/main"]
      exclude = []
    }
  }

  rules {
    creation = true
    update   = true
    deletion = true
  }
}
