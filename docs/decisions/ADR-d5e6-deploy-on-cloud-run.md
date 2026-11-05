<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-d5e6: Deploy on Google Cloud Run](#adr-d5e6-deploy-on-google-cloud-run)
  - [Context](#context)
  - [Decision](#decision)
  - [Alternatives rejected](#alternatives-rejected)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-d5e6: Deploy on Google Cloud Run

- Status: accepted
- Date: 2026-11-05

## Context

The app must run in the cloud, dockerized, at the lowest cost, and run the same way locally.

## Decision

- Google Cloud Run, one service, `max-instances=1`, scale to zero.
- GitHub Actions deploys from `main` with Workload Identity Federation. No service account keys.
- The deploy service account gets only what deploying needs. A separate runtime service account runs the app.
- A budget alert on the project. It warns, it does not stop spending.

## Alternatives rejected

- Fly.io: no free plan for new accounts.
- Render free tier: sleeps after 15 minutes, free Postgres expires.
- A VPS: free or cheap, but the server must be patched by hand.

## Consequences

- The kata uses in-memory repositories. Data is lost when the instance scales to zero or restarts.
  `max-instances=1` keeps one consistent view of the data. Persistence is a later ticket.
