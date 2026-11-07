<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
## Contents

- [Agent instructions](#agent-instructions)
  - [Rules](#rules)
  - [Writing style for docs](#writing-style-for-docs)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Agent instructions

Read `README.md` for layout and commands. Decisions are in `docs/decisions/`.
All work is tracked in Jira space `CHB` (see `TODO.md`). Move tickets only as `docs/process/kanban.md` defines. Never track work in files outside this repository or Jira.

## Rules

- Learnings go in this repository, never in personal agent memory. See `docs/decisions/ADR-eedb-learnings-live-in-the-repo.md`.
- A correction becomes a hook or a check before it becomes prose.
- A dependency update that breaks something gets an automated test in the fix. See `docs/knowledge/dependency-update-breaks-get-a-test.md`.
- Work outside-in: an acceptance test first, then unit tests, one public method at a time.
- Branch from `main`. Start dependent work after the parent PR merges.
  A PR that must build on an open PR uses `--base <parent-branch>`, never `--base main`. GitHub retargets it to `main` when the parent merges.
- Push branches and open PRs through `factory/bin/as-agent` (for example `factory/bin/as-agent gh pr create`), never with the owner's login. See ADR-d7b7.
- Commit only through `factory/bin/as-agent` too. If it fails (for example no 1Password service account token in the keyring, exit code 3), stop and report.
  Never commit with another git identity, not even locally.
- Do every Jira write (create, transition, comment, edit-comment, assign, rank, section, description, priority) through `factory/bin/jira`. It acts as the Atlassian account "Claude (CHB agent)".
  Write Jira text in Markdown. The wrapper renders headings, lists, `code` and bold, and links CHB keys and PR numbers.
  Every ticket gets High, Medium or Low as `docs/process/priorities.md` defines.
  Never write to Jira with `acli`: its login is the owner's. Read with `factory/bin/jira view` and `factory/bin/jira search`.
  A PreToolUse hook (`.claude/settings.json`, `factory/hooks/deny_raw_jira_writes.py`) blocks `acli jira` writes and `curl`, `wget` or Python writes to the Jira REST API.
  It matches the whole command text: put a commit message or PR body that quotes such a command in a file.
  If `factory/bin/jira transition` exits 4, a board rule blocks the move (WIP limit or missing section). Leave the ticket, comment, and report. See `docs/process/kanban.md`.
  If `factory/bin/jira` fails (for example no 1Password service account token in the keyring, exit code 3), stop and report.
- Run `make check` before every commit. Gates read exit codes, not output text.
- One Jira ticket per branch: `CHB-<n>-<slug>`. Every change reaches `main` through a squash-merged PR: the `main` ruleset applies to everyone (`infra/github/ruleset.tf`).
- Kata design rules: services stay stateless, no circular dependencies between services, data in repositories, packages follow actors and areas.
- Do not edit `.claude/`, `factory/` or `.github/` as part of a feature ticket.

## Writing style for docs

Short declarative sentences. Concrete nouns. No filler adjectives.
