<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-50e4: Authentication with Identity Platform, authorization in the app](#adr-50e4-authentication-with-identity-platform-authorization-in-the-app)
  - [Context](#context)
  - [Decision](#decision)
  - [Alternatives rejected](#alternatives-rejected)
  - [Consequences](#consequences)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-50e4: Authentication with Identity Platform, authorization in the app

- Status: accepted
- Date: 2026-11-06

## Context

The app must stay public: anyone can sign up and use it.
The kata has admin actions (hotels, employees, booking policies).
Agents write most of the code, so a missed permission check must fail CI, not reach production.

## Decision

- Google Identity Platform (Firebase Auth) handles sign-up and login: Google accounts and email with password.
  These are Tier 1 providers, free up to 49,999 monthly active users.
  Phone sign-in and SMS MFA stay off: they are charged per message.
- The frontend gets an ID token from the Firebase SDK. FastAPI verifies it on every request.
- Every endpoint is closed by default. Public paths are listed explicitly: `/api/health`, `/api/docs`, `/api/redoc`, `/api/openapi.json`, the frontend.
- Roles live in the app: employee, company admin, hotel admin. A new account gets no company and no admin role.
- A test matrix calls every endpoint with every role and with no token. CI fails on any unexpected success.
- Identity Platform configuration is in Terraform.

## Alternatives rejected

- IAP on Cloud Run: only accounts on an allow list get in, and it also blocks the health check.
- Own password storage: hashing, resets and lockout become our code to secure.
- Auth0 or Clerk: another vendor outside Google Cloud and Terraform.

## Consequences

- OWASP ASVS level 2 is the target. Body size limits and rate limits protect the in-memory repositories.
