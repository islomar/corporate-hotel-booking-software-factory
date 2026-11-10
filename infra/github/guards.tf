# Settings Terraform cannot manage, checked on every plan (CHB-110).
# `make infra-plan` fails when a guard is false. Fix the setting, then plan again.

# Private vulnerability reporting (SECURITY.md points to its form). Set by hand on 2026-11-08
# (CHB-26): provider 6.13.0 has no resource for it. When integrations/terraform-provider-github
# PR #2969 ships one, manage it as a resource and drop this guard.
data "github_rest_api" "private_vulnerability_reporting" {
  endpoint = "repos/${github_repository.this.full_name}/private-vulnerability-reporting"
}

# CodeQL default setup (CHB-134). Set by hand on 2026-11-08: provider 6.13.0 has no resource
# for it. The guard checks `state` only: a fresh setup lists no languages until its first
# analysis finishes.
data "github_rest_api" "codeql_default_setup" {
  endpoint = "repos/${github_repository.this.full_name}/code-scanning/default-setup"
}

# Repository-level Actions secrets reach any workflow, including one edited on an agent branch.
# Secrets belong in an environment limited to main (ADR-d7b7, CHB-86). Names only: the data
# source never reads values.
data "github_actions_secrets" "repository" {
  name = github_repository.this.name
}

resource "terraform_data" "guards" {
  lifecycle {
    precondition {
      condition     = try(jsondecode(data.github_rest_api.private_vulnerability_reporting.body).enabled, false) == true
      error_message = "Private vulnerability reporting is off. Turn it on: GitHub > Settings > Advanced Security > Private vulnerability reporting > Enable (CHB-26)."
    }
    precondition {
      condition     = try(jsondecode(data.github_rest_api.codeql_default_setup.body).state, "") == "configured"
      error_message = "CodeQL default setup is off (state: ${try(jsondecode(data.github_rest_api.codeql_default_setup.body).state, "unreadable response")}). Turn it on: GitHub > Settings > Advanced Security > Code scanning > CodeQL analysis > Set up > Default > Enable CodeQL (CHB-134)."
    }
    precondition {
      condition     = length(data.github_actions_secrets.repository.secrets) == 0
      error_message = "The repository has Actions secrets at repository level: ${join(", ", [for s in data.github_actions_secrets.repository.secrets : s.name])}. Move them to an environment limited to main (ADR-d7b7)."
    }
  }
}
