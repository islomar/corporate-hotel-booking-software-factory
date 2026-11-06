<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Infrastructure](#infrastructure)
  - [Use](#use)
  - [Rules](#rules)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Infrastructure

Terraform manages the infrastructure. See ADR-aae8.

| Folder | Scope | Credentials | State |
|---|---|---|---|
| `gcp/` | Google Cloud: APIs, registry, service accounts, Workload Identity, budget, Cloud Run | your gcloud login (configuration `chb`) | `gs://chb-factory-a92932-tfstate/gcp` |

The state bucket was created by hand (CHB-10) and is imported into `gcp/`.

## Use

Requirements: [tfenv](https://github.com/tfutils/tfenv) (reads `.terraform-version`), gcloud with the `chb` configuration.

```sh
make infra-plan    # writes infra/gcp/gcp.tfplan
make infra-apply   # applies exactly that plan
```

Until CI applies (CHB-13), the owner applies locally.

## Rules

- IAM is additive only (`*_iam_member`). Never manage project IAM authoritatively: it would remove the owner and Google's service agents.
- `prevent_destroy` protects the service accounts, the Workload Identity pool and provider, the registry, the state bucket and the Cloud Run service.
  Replacing a service account changes its ID and breaks Workload Identity.
- Secrets never go into Terraform: their values would be stored in the state.
- The billing account ID is a sensitive variable, filled from gcloud at run time. Never commit it.
- The deploy workflow changes only the Cloud Run image. Terraform ignores the image.
- A plan with an unexpected destroy or replace is a bug in the code. Do not apply it.
