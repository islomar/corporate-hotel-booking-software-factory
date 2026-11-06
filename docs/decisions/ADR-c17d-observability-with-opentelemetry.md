<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-c17d: Observability with OpenTelemetry](#adr-c17d-observability-with-opentelemetry)
  - [Context](#context)
  - [Decision](#decision)
  - [Alternatives rejected](#alternatives-rejected)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-c17d: Observability with OpenTelemetry

- Status: accepted
- Date: 2026-11-06

## Context

Logs, traces and metrics are needed from the first feature.
The agent must read production telemetry from a CLI: the `validating` step and the evidence per acceptance criterion depend on it.

## Decision

- The backend uses the OpenTelemetry SDK. Instrumentation is vendor-neutral.
- Production: JSON logs with trace IDs to Cloud Logging, spans to Cloud Trace, metrics to Cloud Monitoring.
  Uptime check, SLO and alert policies are in Terraform.
- Local: `make dev` runs the `grafana/otel-lgtm` container. Same instrumentation, local Grafana.
- Cloud Run throttles CPU between requests. The app flushes telemetry per request instead of paying for always-allocated CPU.
- Factory telemetry stays separate from app telemetry: Claude Code's OpenTelemetry export and the orchestrator's per-ticket data go to the local Grafana. Evals use it.

## Alternatives rejected

- Grafana Cloud free tier: better dashboards and frontend monitoring, but a second vendor, token and alerting system. It is the upgrade path.
- Datadog: the free tier has no APM and short retention.
- Sentry for frontend errors: decide when the frontend has real flows.

## Consequences

- The OpenTelemetry packages install on Python 3.15.2 (checked 2026-11-06).
- The agent reads logs and traces with `gcloud logging read` and the Cloud Trace API, through a read-only identity.
