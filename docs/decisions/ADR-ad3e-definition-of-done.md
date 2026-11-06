<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-ad3e: Definition of done](#adr-ad3e-definition-of-done)
  - [Decision](#decision)
  - [Source](#source)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-ad3e: Definition of done

- Status: accepted
- Date: 2026-11-05

## Decision

Jira workflow: To Do, In Progress, In Review, Validating, Done, Discarded.

- Merged is not done. After merge, a ticket moves to Validating. `docs/process/kanban.md` defines every status and its rules.
- A ticket is Done when its acceptance criteria are checked in the deployed system.
- Discarded is a valid outcome. Record why.
- Every ticket states its "why" as a hypothesis.

## Source

eferro, "Turning up the dial": the `validating` and `discarded` states.
