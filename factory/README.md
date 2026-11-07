<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Factory](#factory)
  - [Credentials: the 1Password service account](#credentials-the-1password-service-account)
    - [Renewing the token](#renewing-the-token)
  - [Acting as the agent](#acting-as-the-agent)
  - [Acting in Jira as the agent](#acting-in-jira-as-the-agent)
    - [Board rules on transition](#board-rules-on-transition)
    - [Hook against raw Jira writes](#hook-against-raw-jira-writes)
  - [Hook: commands for the owner work from any folder](#hook-commands-for-the-owner-work-from-any-folder)
  - [Automated PR review (shadow mode)](#automated-pr-review-shadow-mode)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Factory

Agent workflows, the orchestrator and the notifier live here. Built in phases 3 to 7.

## Credentials: the 1Password service account

`factory/bin/as-agent` and `factory/bin/jira` read their secrets from the 1Password vault `chb-factory` (CHB-97).
It holds two items: the document `chb-factory-agent.2026-11-06.private-key` (the GitHub App key) and the API credential `chb-jira-claude.api-token` (the Jira email and token).

They read it as the 1Password service account `chb-factory-agent`, which can only read that vault.
Its token is in the GNOME keyring, unlocked at login:

```sh
secret-tool lookup service chb-factory key op-service-account-token
```

`factory/op_service_account.py` reads it on every call and passes it as `OP_SERVICE_ACCOUNT_TOKEN` in the environment of the one `op` subprocess.
It never reaches the parent shell, the command that as-agent runs, a file or the output.
`op` then never asks the owner's desktop app, so no approval pop-up appears and the owner's other vaults are out of reach.

Without the token (keyring locked, item absent, `secret-tool` missing) or when `op` fails (token expired or revoked), `jira` exits 3 and `as-agent` exits 3 and runs nothing.
Neither falls back to the owner's desktop 1Password: that would bring back the pop-ups and the owner's full 1Password rights.
The agent stops and reports.
`AGENT_APP_KEY_COMMAND` replaces the `op` call of as-agent with another command, as an explicit opt-in. That command never gets the service account token.

The pop-up was also a human check on every read. Without it, any process that runs as the owner's user while the keyring is unlocked can read the two items in `chb-factory`, and nothing else.

### Renewing the token

A service account token can expire. When it expires, every agent commit, push and Jira write stops with exit 3.
The owner checks the expiry in 1Password (<https://my.1password.com> > Developer > Service accounts > `chb-factory-agent`) and renews it before that date:

1. Create a new token for `chb-factory-agent` in the same place, with the same vault access (`chb-factory`, Read Items only).
2. In your own terminal, store it under the same attributes; this replaces the old one:
   `secret-tool store --label="chb-factory 1Password service account" service chb-factory key op-service-account-token`
3. Check: `factory/bin/jira view CHB-97` exits 0.
4. Revoke the old token in 1Password.

Ticket: CHB-121.

## Acting as the agent

`factory/bin/as-agent <command>` runs a command as the GitHub App `chb-factory-agent`:
`gh` uses a one-hour installation token, git commits are authored by `chb-factory-agent[bot]`, and git pushes over HTTPS with the token.
The private key is read at run time from the vault `chb-factory` with the service account (see above).

```sh
factory/bin/as-agent git push -u origin CHB-71-agent-token
factory/bin/as-agent gh pr create --fill
```

## Acting in Jira as the agent

`factory/bin/jira` calls the Jira REST API of `islomar.atlassian.net` as the Claude account ("Claude (CHB agent)"), CHB-107.
Its email and API token are read from the 1Password item `chb-jira-claude.api-token` (fields `username` and `credential`, vault `chb-factory`) on every call, with one `op` call as the service account.
The Jira token stays inside the Python process: never printed, written to a file or passed to another command.
The owner's `acli` login is global state and is not touched.

```sh
factory/bin/jira view CHB-12
factory/bin/jira search 'project = CHB AND status = "In Review"'
factory/bin/jira create --parent CHB-27 --summary "Short title" --description-file description.txt --priority Medium
factory/bin/jira transition CHB-12 "In Review"
factory/bin/jira comment CHB-12 --body-file comment.txt   # "-" reads stdin
factory/bin/jira assign CHB-12 --me
factory/bin/jira rank CHB-12 CHB-14 --before CHB-9   # or --after; up to 50 issues, kept in that order
factory/bin/jira section CHB-12 --heading "Acceptance criteria (Validating)" --body-file section.txt
factory/bin/jira priority CHB-12 High   # High, Medium or Low (docs/process/priorities.md)
factory/bin/jira description CHB-12 --file description.md   # replaces the whole description
factory/bin/jira edit-comment CHB-12 10071 --body-file comment.md   # the agent's own comments only
```

Text files are plain text. Blank lines separate paragraphs and URLs become links.

`rank` calls `PUT /rest/agile/1.0/issue/rank`. When Jira ranks only some of the issues (HTTP 207), it exits 1 and names each issue that failed.

`section` reads the description and finds the top-level heading with that exact text.
It replaces the section up to the next heading of the same or a higher level, or appends the section when the heading is absent.
The new section is a level-2 heading and the body. In the body, lines that start with a dash and a space become a bullet list.
It sends every other part of the description back unchanged. It refuses a description with two such headings.
It reads, then writes: it overwrites an edit that someone else makes between the two requests.
A bad heading, an empty body, a duplicate key or more than 50 issues exit 2 before the wrapper reads 1Password.

`view` shows a bullet list as lines that start with a dash.

Exit codes: 0 done, 1 Jira refused or failed, 2 usage error, 3 no credential (no service account token in the keyring, or `op` failed), 4 a board rule blocks the transition.
On 3 the agent stops and reports. On 4 the ticket does not move: the agent comments on it and reports.

The account is a CHB project Administrator, not a Member: Jira Free has no Member role.
It can delete issues and change project and board settings through the REST API.
The wrapper is the guardrail:

- it accepts only `CHB-<n>` keys and creates only Tasks under a CHB Epic;
- `rank` changes only the rank of issues, and `section` writes only the description field;
- it has no delete, no project, board, workflow or permission write, and no raw request command;
- it refuses HTTP redirects, so the token is never sent to another host.

Do not add a generic request command or a delete command.
The Jira token itself still has the Jira account's full rights. Anyone who reads it bypasses the wrapper.
The 1Password service account narrows who can read it to processes that can read the keyring item, but does not narrow what the Jira token can do.

### Board rules on transition

`before_transition` in `jira.py` runs before every transition (CHB-108). It exits 4 and does not move the ticket when:

- the target status is Validating and the description has no section headed "Acceptance criteria (Validating)" with text under it;
- the target status is Needs human and the description has no section headed "Needs human: owner steps" with text under it;
- the target column has a max on the board and already holds that many other CHB tickets. The message names the column, the limit and the tickets.

The limits come from `GET /rest/agile/1.0/board/2/configuration` on every call, never from code.
The count is a JQL search on the column's status ids in project CHB, without the moved ticket.
Moves within a column, out of a column, or into a column without a max are not limited. A failed board read exits 1 and does not move the ticket.
After a move out of In Progress that leaves it below its board min, `transition` prints `In Progress is below its minimum (0/1): idle, ask the owner for the next ticket` and exits 0.
If that count fails, it prints a notice instead and still exits 0: the ticket has moved.
Two agents that move tickets at the same moment can both pass the check. The owner overrides a limit in the Jira UI, with a comment.

### Hook against raw Jira writes

`.claude/settings.json` runs `factory/hooks/deny_raw_jira_writes.py` before every Bash command of a Claude Code session started in this repository.
It exits 2, which denies the command, for:

- an `acli jira` command that does not only read. Reads are view, search, list, get and `auth status`. The hook denies any other command, known or not, and `--help` on a write subcommand;
- `curl`, `wget` or Python calls to `*.atlassian.net` or a `/rest/api` or `/rest/agile` path that send data or use a method other than GET or HEAD.

The hook is a guardrail, not a boundary: it reads the command text only. A script file that calls the API, or a URL hidden in a variable, passes.
It matches anywhere in the command, so it also denies a commit message that quotes a denied command. Use a message file.
An error inside the hook denies the command. If `python3` cannot start, the command goes through: Claude Code treats that as a non-blocking error.
Sessions started in another directory do not load this hook.

## Hook: commands for the owner work from any folder

`.claude/settings.json` runs `factory/hooks/check_owner_commands.py` when the agent finishes a reply (Stop hook, CHB-126).
On 2026-11-07 the agent gave the owner `cp <backup> .git/config`. The owner ran it from another repository's folder, and that repository's config was overwritten.

The hook reads the shell code blocks of the reply (no language, or `sh`, `bash`, `shell`, `zsh`, `console`). It sends the agent back to rewrite a command that depends on the current folder:

- `git` or `make` without `-C`, or a `cd` to a relative folder;
- a program called by a relative path, such as `factory/bin/jira`;
- `cp`, `mv`, `rm`, `ln`, `tee`, `touch`, `mkdir`, `chmod`, `chown`, `truncate`, `install`, `rsync`, `rmdir`, `sed -i`, `>` or `>>` with a relative path.

These pass: a command after `cd /absolute/folder` in the same block, a line that starts with `!` (Claude Code runs it in the session folder), inline code, and any block in another language.
Put a block that you do not want pasted in a `text` block.

The hook blocks once per reply and never on bad input, so it cannot loop. Like the Jira hook, it is a guardrail that reads text only.

## Automated PR review (shadow mode)

`.github/workflows/review.yml` scores every same-repository PR into `main` with Claude Code and posts a check run named `review` as the App `chb-factory-reviewer` on the PR head commit.
The `main` ruleset does not require the check yet. Design: ADR-ea8c (PR #16). Tickets: CHB-87, CHB-93, CHB-105.

How it starts:

1. `.github/workflows/review-trigger.yml` runs on `pull_request`. It has no permissions and no secrets, and only echoes a line.
2. When it completes, `review.yml` runs on `workflow_run`. GitHub takes it from `main`, so the workflow, gate, schema and prompt always come from `main`.
3. Its `resolve` job takes the head SHA from the event and finds the PR through the API.
   `review/resolve_pr.py` checks it: open, not draft, into `main`, from this repository, not Dependabot, head equal to that SHA.
4. Its `score-pr` job runs in the `reviewer` environment, reads the PR head as data and never runs it.
5. `review/plan.py` decides from paths and diff size, never from PR text:
   - every changed file is docs: a `neutral` check, no Claude call;
   - only docs changed since the last commit the App reviewed: that verdict is posted again on the new head, no Claude call;
   - otherwise Claude reviews: Opus for harness, supply-chain or governance paths, more than 400 changed lines or a binary file outside `docs/`; Sonnet for the rest.

Claude authenticates with the owner's subscription token (`claude setup-token`), not an API key.
`--bare` cannot read that token, so the job isolates Claude with `--safe-mode`, `--restricted`, an empty `HOME` and environment switches instead.
A `CLAUDE.md`, `AGENTS.md`, rule, skill, hook or `.mcp.json` in the PR head never loads. ADR-ea8c decision 2 lists each switch and the canary test behind it.
`--max-budget-usd 1` stops a runaway run.

A change to `review.yml` takes effect only after merge: the PR that changes it is reviewed by the copy on `main`.

| File | Role |
|---|---|
| `review/prompt.md` | Review instructions. PR text and code are untrusted data. |
| `review/schema.json` | Output shape: five dimensions, each with a score and findings, plus a summary. |
| `review/gate.py` | Validates the output, recomputes the scores, decides, and builds the check run. |
| `review/resolve_pr.py` | Picks the PR to review from API data: open, not draft, into `main`, same repository, not Dependabot, head equal to the run's SHA. |
| `review/plan.py` | Skip, carry forward or review, and which model. Holds the docs allowlist. |

The gate ignores every score the model claims. Each dimension starts at 10 and loses 3 per critical finding, 2 per improvement and 0.5 per cleanup.
A critical finding caps its dimension at 7. Merge Safety is the weighted mean: code quality 30, security 25, business alignment 15, tests 15, architecture 15.
The check passes when Merge Safety is at least 8.5 and no finding is critical.
Malformed JSON, a schema violation, an extra field, an error result or a non-zero exit of `claude` fail the check.

The summary also shows the risk class from `git diff --no-renames --name-only base...head` and the round number from earlier `review` runs of the App.
Skipped and carried checks do not count as rounds. A failed run without a valid review (Claude error, usage limit) is never carried forward.

The workflow needs three secrets in the `reviewer` environment, which admits `main` only: `CLAUDE_CODE_OAUTH_TOKEN` (CHB-105), `REVIEWER_APP_CLIENT_ID` and `REVIEWER_APP_PRIVATE_KEY` (CHB-85).
Do not store them as repository secrets: a branch can add a `pull_request` workflow that reads repository secrets.
Without them the job prints a notice and posts nothing.

Run the gate on a saved result:

```sh
python3 -I factory/review/gate.py --result result.json --claude-exit-code 0 --schema factory/review/schema.json \
  --changed-files names.z --prior-runs runs.json --app-slug chb-factory-reviewer --head-sha HEAD_SHA --out check-run.json
```
