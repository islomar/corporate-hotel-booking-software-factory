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
