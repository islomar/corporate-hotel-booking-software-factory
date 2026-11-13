# Guards with stubbed GitHub responses (CHB-134). No credentials, no network.
# Run from infra/github: terraform init -backend=false && terraform test
# The first run is the control: every guard passes. Each later run turns one guard off.

mock_provider "github" {}

# A mock provider cannot import. These are the targets of the import blocks.
override_resource {
  target = github_repository.this
  values = { full_name = "islomar/corporate-hotel-booking-software-factory" }
}
override_resource { target = github_repository_vulnerability_alerts.this }
override_resource { target = github_repository_dependabot_security_updates.this }
override_resource { target = github_actions_variable.this }
override_resource { target = github_actions_repository_permissions.this }
override_resource { target = github_workflow_repository_permissions.this }
override_resource { target = github_repository_environment.this }
override_resource { target = github_issue_label.this }

override_data {
  target = data.github_rest_api.private_vulnerability_reporting
  values = { body = "{\"enabled\":true}" }
}

override_data {
  target = data.github_actions_secrets.repository
  values = { secrets = [] }
}

override_data {
  target = data.github_rest_api.codeql_default_setup
  values = { body = "{\"state\":\"configured\",\"languages\":[\"actions\",\"javascript-typescript\",\"python\"]}" }
}

run "all_guards_pass" {
  command = plan
}

run "codeql_default_setup_off_fails_the_plan" {
  command = plan

  override_data {
    target = data.github_rest_api.codeql_default_setup
    values = { body = "{\"state\":\"not-configured\",\"languages\":[]}" }
  }

  expect_failures = [terraform_data.guards]
}

run "private_vulnerability_reporting_off_fails_the_plan" {
  command = plan

  override_data {
    target = data.github_rest_api.private_vulnerability_reporting
    values = { body = "{\"enabled\":false}" }
  }

  expect_failures = [terraform_data.guards]
}

run "repository_secret_fails_the_plan" {
  command = plan

  override_data {
    target = data.github_actions_secrets.repository
    values = { secrets = [{ name = "LEAKED", created_at = "", updated_at = "" }] }
  }

  expect_failures = [terraform_data.guards]
}

# CHB-173: a tag named like a protected branch cannot be created, moved or deleted by anyone but the Admin role.
run "branch_named_tags_are_locked" {
  command = plan

  assert {
    condition     = github_repository_ruleset.branch_named_tags.target == "tag" && github_repository_ruleset.branch_named_tags.enforcement == "active"
    error_message = "The branch-named-tags ruleset must target tags and be active (CHB-173)."
  }
  assert {
    condition     = contains(github_repository_ruleset.branch_named_tags.conditions[0].ref_name[0].include, "refs/tags/main")
    error_message = "The branch-named-tags ruleset must cover refs/tags/main (CHB-173)."
  }
  assert {
    condition = alltrue([
      github_repository_ruleset.branch_named_tags.rules[0].creation,
      github_repository_ruleset.branch_named_tags.rules[0].update,
      github_repository_ruleset.branch_named_tags.rules[0].deletion,
    ])
    error_message = "The branch-named-tags ruleset must block creation, update and deletion (CHB-173)."
  }
  assert {
    condition     = [for a in github_repository_ruleset.branch_named_tags.bypass_actors : "${a.actor_type}:${a.actor_id}"] == ["RepositoryRole:5"]
    error_message = "Only the Admin role may bypass the branch-named-tags ruleset (CHB-173)."
  }
}

# CHB-177: a PR is tested against the latest main before it can merge. Without this, two PRs
# that are green alone can merge together and break main (#105 and #106, 2026-11-10).
run "main_ruleset_requires_up_to_date_branches" {
  command = plan

  assert {
    condition     = github_repository_ruleset.main.rules[0].required_status_checks[0].strict_required_status_checks_policy == true
    error_message = "The main ruleset must require branches to be up to date before merge (CHB-177)."
  }
}

run "repository_topics_fit_github_and_name_the_community" {
  command = plan

  assert {
    condition     = length(github_repository.this.topics) <= 20
    error_message = "GitHub allows at most 20 repository topics; the apply fails above that."
  }
  assert {
    condition     = alltrue([for t in ["kanban", "lean", "lean-software-development"] : contains(github_repository.this.topics, t)])
    error_message = "The topics must include kanban, lean and lean-software-development (CHB-186)."
  }
  assert {
    condition     = !anytrue([for t in ["codurance", "corporate-hotel-booking", "kata"] : contains(github_repository.this.topics, t)])
    error_message = "codurance, corporate-hotel-booking and kata were removed on the owner's request (CHB-186)."
  }
}
