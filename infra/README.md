<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Infrastructure](#infrastructure)
  - [Use](#use)
    - [Destroy and replace](#destroy-and-replace)
  - [Sign-in](#sign-in)
  - [Rules](#rules)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Infrastructure

Terraform manages the infrastructure. See ADR-aae8.

| Folder | Scope | Credentials | State |
|---|---|---|---|
| `gcp/` | Google Cloud: APIs, registry, service accounts, Workload Identity, budget, Cloud Run, monitoring (uptime check, SLO, alert policies, email channel), Identity Platform (email and password sign-in, browser API key) | your gcloud login (configuration `chb`) | `gs://chb-factory-a92932-tfstate/gcp` |
| `github/` | Repository settings, Actions variables and permissions (allow list, SHA pinning), the `main` ruleset, deployment environments, the Dependabot labels, the Jira autolink, and guards for what it cannot manage (`guards.tf`) | your `gh auth token` (admin) | `gs://chb-factory-a92932-tfstate/github` |

The state bucket was created by hand (CHB-10) and is imported into `gcp/`.

## Use

Requirements: [tfenv](https://github.com/tfutils/tfenv) (reads `.terraform-version`), gcloud with the `chb` configuration.

The alerts go to an email address that is not in the repository.
Export it in the shell that runs `make infra-plan`, for example from 1Password:

```sh
export TF_VAR_alert_email="$(op read 'op://<vault>/<item>/<field>')"
```

Never write the address in a file that git tracks, dotfiles included.

`make infra-plan` stops without it. `make infra-apply` reads it from the saved plan.

Until CI applies (CHB-13), the owner applies locally, after the PR with the Terraform change merges:

1. Update `main`: `git switch main && git pull --ff-only`.
2. Run `make infra-plan`. It writes `infra/.plans/gcp.tfplan` and `infra/.plans/github.tfplan`, and prints a summary per folder: add, change, destroy, replace and forget counts.
3. Read the plan output and the summary. They must match the merged PR.
4. Run `make infra-apply`. It applies the saved plans, nothing else, and deletes them when the apply succeeds.

`scripts/infra_guard.py` stops both targets when one of these is true. It checks before it asks gcloud for a token.

- the checkout is not `main`;
- `main` is behind or ahead of `origin/main` (it runs `git fetch` first);
- `infra/`, `Makefile` or `scripts/` have uncommitted or untracked files.

Changes in other folders, for example `frontend/` or untracked notes, do not stop it.
The message names the fix command.

`make infra-apply` also stops when a saved plan is missing, comes from another commit, or changed after `make infra-plan` wrote it.
Run `make infra-plan` again.

### Destroy and replace

A plan that destroys or replaces a resource fails both targets, in red, with the addresses.
On 2026-11-06 a plan from an outdated branch showed 4 destroys of live resources (CHB-99).
An unexpected destroy almost always means the checkout misses merged code. Do not override it.

When you intend every destroy, list the exact addresses, comma-separated, for both targets.
Use single quotes: addresses contain `"` and `[`.

```sh
make infra-plan ALLOW_DESTROY='github_repository_environment.env["staging"],google_x.y'
make infra-apply ALLOW_DESTROY='github_repository_environment.env["staging"],google_x.y'
```

The list must match exactly. A missing or an extra address fails.

## Sign-in

`gcp/identity_platform.tf` turns on Identity Platform with email and password sign-in (ADR-50e4, CHB-20).
Phone sign-in and SMS MFA stay off: Google charges per SMS.
It creates a browser API key, restricted to the sign-in APIs and to the app's pages.
Cloud Run gets the key and the auth domain as environment variables; the backend serves them at `/auth-config.json`.
The key is public by design: it names the project, it grants nothing.

The Google provider is not in Terraform: its resource needs the OAuth client secret, and Terraform would store it in the state.
The owner creates the OAuth client and adds the provider in the console, once, after `make infra-apply`:

1. Consent screen: open <https://console.cloud.google.com/auth/branding?project=chb-factory-a92932>.
   If it says "Google Auth platform not configured yet", click **Get Started**: app name `Corporate Hotel Booking`, your support email, **Next**,
   audience **External**, **Next**, your contact email, **Next**, accept the User Data Policy, **Continue**, **Create**.
   Google does not let you remove a consent screen once configured.
2. Publish: open <https://console.cloud.google.com/auth/audience?project=chb-factory-a92932>, **Publish app**, confirm.
   In "Testing" only listed test users can sign in. The app asks only for name, email and profile: no Google verification.
3. OAuth client: open <https://console.cloud.google.com/auth/clients?project=chb-factory-a92932>, **Create client**.
   Type **Web application**, name `chb-identity-platform`.
   Authorized JavaScript origins: `https://chb-1027266991110.europe-west1.run.app` and `http://localhost:5173`.
   Authorized redirect URIs: `https://chb-factory-a92932.firebaseapp.com/__/auth/handler`. **Create**.
   Copy the client ID and the client secret into a new 1Password item. Nowhere else.
4. Provider: open <https://console.cloud.google.com/customer-identity/providers?project=chb-factory-a92932>, **Add a provider**, **Google**.
   Paste the Web Client ID and the Web Client Secret, **Save**.
5. Check: <https://chb-factory-a92932.firebaseapp.com/__/auth/handler> answers with a page, not a 404. The Google popup needs it; email and password does not.
6. Check: <https://console.cloud.google.com/apis/credentials?project=chb-factory-a92932> lists one browser key, `CHB browser sign-in (Terraform)`.
   Google warns against two keys restricted to websites. Report any key created by Identity Platform; do not delete it without a ticket.

If `make infra-apply` fails on `google_identity_platform_config` with a message about enabling Identity Platform:
open <https://console.cloud.google.com/marketplace/details/google-cloud-platform/customer-identity?project=chb-factory-a92932>, click **Enable Identity Platform**, then run `make infra-plan` again.
The plan may then want to import the config: report it, do not apply.

Until `make infra-apply` has run, the app runs signed out and every closed `/api` route answers 401.

To sign in from `make dev` against the production project:

```sh
export FIREBASE_PROJECT_ID=chb-factory-a92932
export FIREBASE_AUTH_DOMAIN=chb-factory-a92932.firebaseapp.com
export FIREBASE_API_KEY="$(terraform -chdir="$HOME/workspace/corporate-hotel-booking-software-factory/infra/gcp" output -json firebase_web_config | jq -r .apiKey)"
make -C ~/workspace/corporate-hotel-booking-software-factory dev
```

## Rules

- IAM is additive only (`*_iam_member`). Never manage project IAM authoritatively: it would remove the owner and Google's service agents.
- `prevent_destroy` protects the service accounts, the Workload Identity pool and provider, the registry, the state bucket and the Cloud Run service.
  Replacing a service account changes its ID and breaks Workload Identity.
- Secrets never go into Terraform: their values would be stored in the state.
- The billing account ID is a sensitive variable, filled from gcloud at run time. Never commit it.
- The deploy workflow changes only the Cloud Run image. Terraform ignores the image.
- A plan with an unexpected destroy or replace is a bug in the code. Do not apply it.
