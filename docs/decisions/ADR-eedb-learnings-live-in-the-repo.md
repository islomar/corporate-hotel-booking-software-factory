<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ADR-eedb: Learnings live in the repository](#adr-eedb-learnings-live-in-the-repository)
  - [Context](#context)
  - [Decision](#decision)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ADR-eedb: Learnings live in the repository

- Status: accepted
- Date: 2026-11-05

## Context

Agent harnesses store learnings in per-user memory. Other people and other sessions never see it.
Prose rules get ignored. Rules enforced by hooks and checks get followed.

## Decision

- Every finding from a postmortem or a correction becomes, in order of preference:
  1. a hook, check or tool;
  2. a change to a skill or workflow;
  3. a document in `docs/knowledge/`;
  4. rejected, with the reason recorded.
- No instruction file points to personal agent memory.
- Changes to the harness (`.claude/`, `factory/`, `.github/`) need human review.
- Gates trust exit codes, never output text.
