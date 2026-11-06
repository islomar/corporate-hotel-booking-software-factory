<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
## Contents

- [Kanban statuses](#kanban-statuses)
  - [Flow](#flow)
  - [Statuses](#statuses)
    - [To Do](#to-do)
    - [In Progress](#in-progress)
    - [In Review](#in-review)
    - [Validating](#validating)
    - [Needs human](#needs-human)
    - [Done](#done)
    - [Discarded](#discarded)
  - [Assignee](#assignee)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Kanban statuses

The board is the Jira space `CHB`.
Each status below has a meaning, rules to enter and rules to leave.
A ticket moves only when the rules to leave are true. Agents follow this document; the owner can override it, and the override is written as a comment on the ticket.

ADR-ad3e defines "done". This document makes it operational.

## Flow

```text
To Do → In Progress → In Review → Validating → Done
              │             │            │
              └─────────────┴────────────┴──→ Needs human → (back to where it was)
any status ──→ Discarded
```

## Statuses

### To Do

Meaning: accepted work that nobody has started.

- Enter: the ticket has a summary, a description and a parent epic.
- Leave to In Progress: someone starts working on it. Before that, the ticket has acceptance criteria that can be checked, and nothing it depends on is open.
- WIP limit: none.

### In Progress

Meaning: someone (the agent or the owner) is changing code, infrastructure or documents for this ticket.

- Enter: the worker assigns the ticket to themselves (an agent runs `factory/bin/jira assign CHB-<n> --me`) and creates the branch `CHB-<n>-<slug>` from `main`.
- Leave to In Review: a pull request is open, `make check` passed locally, and the PR body states what merging changes in plain words.
- Leave to Needs human: the worker is blocked by a question or a decision only the owner can make.
- WIP limit: 3, the most agents that run in parallel. Not enforced on the board yet.

### In Review

Meaning: a pull request waits for review and merge.

- Enter: the PR is open and linked from the ticket.
- Leave to Validating: the PR is merged into `main`. CI passed and the required reviews were given.
- Leave to In Progress: review asked for changes.
- WIP limit: 3, set on the board. When the column is full, the agent reviews, fixes or closes PRs before it opens new ones (close before open).

### Validating

Meaning: the change is merged and deployed, but nobody has yet confirmed that it works in the running system. Merged is not done.

- Enter: the PR is merged and, where it applies, deployed or applied.
  For Terraform changes, the owner has run `make infra-apply`.
- Enter: in the same step, the worker writes a section headed "Acceptance criteria (Validating)" in the ticket description, not in a comment.
  A reader without context must see what is still missing. For each criterion not yet checked live, the section states:
  - What must happen, in the live system.
  - When: an absolute date and time, or the event that triggers it (for example "the next Dependabot run, Monday 2026-11-09 05:00 UTC").
  - Where to check: the exact URL or command.
  - Pass condition: what the check must show.
  - Who checks: the agent or the owner. If the owner must act, the section has numbered steps, as for Needs human.
  - If it fails: what happens next, and by when the ticket goes back to In Progress.
- Leave to Done: each acceptance criterion is checked in the live system, and the evidence is written as a ticket comment.
  Evidence is concrete: the smoke test of the deploy, a `gcloud` or `gh api` read of the real setting, a screenshot, a log line, a metric.
  A passing plan or a green CI run is not evidence of the live result.
- Leave to In Progress: the check fails. A new or reopened PR fixes it.
- Some checks need time (for example a background cleanup that runs within a day). The ticket stays in Validating until then.

### Needs human

Meaning: the work cannot continue without the owner.

- Enter: an open question, a decision, an action only the owner can do (a click in a web UI, a sudo command, a payment setting), or a gate that failed and needs judgement.
  The worker writes a section headed "Needs human: owner steps" in the ticket description, not in a comment, with `factory/bin/jira section`.
  It is a numbered step-by-step procedure: what to do, where (exact URL or menu path), how (each click or command).
  It also states what not to touch, the deadline if any, and how the owner reports that it is done.
- Leave: the owner answers or acts. The ticket goes back to the status it came from.
- This status is the top of every notification.

### Done

Meaning: the acceptance criteria hold in the live system, with evidence on the ticket. Nothing more is expected.

- Enter: only from Validating, or directly for tickets with nothing to deploy (a decision, an investigation) once their output exists: a merged ADR, a ticket comment with findings.

### Discarded

Meaning: the ticket will not be done. This is a valid outcome.

- Enter: from any status. A comment states why: superseded, wrong idea, no longer needed.

## Assignee

The person or agent doing the work is the assignee.
Agent work is assigned to the Claude account ("Claude (CHB agent)").

Agents make every Jira write (create, transition, comment, assign) through `factory/bin/jira`, so Jira history shows the Claude account as the author.
`acli` is logged in as the owner and is for the owner only.
When `factory/bin/jira` fails, the agent stops and reports. It never falls back to `acli`.
See `factory/README.md`.
