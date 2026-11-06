<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
## Contents

- [Agent instructions](#agent-instructions)
  - [Rules](#rules)
  - [Writing style for docs](#writing-style-for-docs)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Agent instructions

Read `README.md` for layout and commands. Decisions are in `docs/decisions/`.
All work is tracked in Jira space `CHB` (see `TODO.md`). Never track work in files outside this repository or Jira.

## Rules

- Learnings go in this repository, never in personal agent memory. See `docs/decisions/ADR-eedb-learnings-live-in-the-repo.md`.
- A correction becomes a hook or a check before it becomes prose.
- A dependency update that breaks something gets an automated test in the fix. See `docs/knowledge/dependency-update-breaks-get-a-test.md`.
- Work outside-in: an acceptance test first, then unit tests, one public method at a time.
- Push branches and open PRs through `factory/bin/as-agent` (for example `factory/bin/as-agent gh pr create`), never with the owner's login. See ADR-d7b7.
- Run `make check` before every commit. Gates read exit codes, not output text.
- One Jira ticket per branch: `CHB-<n>-<slug>`. Every change reaches `main` through a squash-merged PR: the `main` ruleset applies to everyone (`infra/github/ruleset.tf`).
- Kata design rules: services stay stateless, no circular dependencies between services, data in repositories, packages follow actors and areas.
- Do not edit `.claude/`, `factory/` or `.github/` as part of a feature ticket.

## Writing style for docs

Short declarative sentences. Concrete nouns. No filler adjectives.
