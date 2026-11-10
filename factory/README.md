<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Factory](#factory)
  - [Credentials: the 1Password service account](#credentials-the-1password-service-account)
    - [Rotating the token](#rotating-the-token)
  - [Acting as the agent](#acting-as-the-agent)
    - [Auto-merge on agent PRs](#auto-merge-on-agent-prs)
  - [Subagent models](#subagent-models)
  - [Acting in Jira as the agent](#acting-in-jira-as-the-agent)
    - [Board rules on transition](#board-rules-on-transition)
    - [Hook against raw Jira writes](#hook-against-raw-jira-writes)
  - [Hook: commands for the owner work from any folder](#hook-commands-for-the-owner-work-from-any-folder)
  - [Hook: no personal agent memory](#hook-no-personal-agent-memory)
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

### Rotating the token

The token does not expire, on purpose: an expiry would stop every agent commit, push and Jira write on a fixed date (owner decision, 2026-11-07).
The token is read-only and reaches only the vault `chb-factory`. The remaining risk is a leak, so the owner rotates the token:

- once a year (next: by 2027-11-07), and
- at once on a suspected leak (the token appeared in a chat, log, file, screenshot or commit).

Owner steps, about 5 minutes:

1. Rotate the token. WHERE: <https://my.1password.com> > Developer > Service accounts > `chb-factory-agent` > Rotate token.
   HOW: for "Expire existing token" pick "in 3 days", or "now" if the token leaked.
   If the screen offers an expiry for the new token, pick "Never".
   Copy the new token. Do not paste it into the Claude chat. Do not change the vault access (`chb-factory`, read only).
2. Store it in your own terminal, never in the Claude chat and never with `!`. This replaces the old one:
   `secret-tool store --label="chb-factory 1Password service account" service chb-factory key op-service-account-token`
   Paste the token at the `Password:` prompt.
3. Check, in your terminal from any folder:
   `~/workspace/corporate-hotel-booking-software-factory/factory/bin/jira view CHB-97` exits 0, and
   `~/workspace/corporate-hotel-booking-software-factory/factory/bin/as-agent gh api /rate_limit --jq .rate.limit` prints a number.
4. If a leak was the reason, tell Claude, so it checks where the token appeared.
5. Tell Claude in the chat: "token rotated". Claude writes the date on the rotation ticket and creates the next yearly rotation ticket.

Tickets: CHB-121, CHB-123.

## Acting as the agent

`factory/bin/as-agent <command>` runs a command as the GitHub App `chb-factory-agent`:
`gh` uses a one-hour installation token, git commits are authored by `chb-factory-agent[bot]`, and git pushes over HTTPS with the token.
The private key is read at run time from the vault `chb-factory` with the service account (see above).

```sh
factory/bin/as-agent git push -u origin CHB-71-agent-token
factory/bin/as-agent gh pr create --fill
```

### Auto-merge on agent PRs

`factory/bin/as-agent gh pr create` (or `gh pr new`) enables auto-merge (squash) on the PR it opens (CHB-120).
The step lives in `factory/as_agent.py` (`run`), so every agent gets it. Tests: `factory/tests/test_as_agent.py`.
It runs `gh pr merge --auto --squash <PR URL>` with the App token.
A merge by the App token triggers the push-to-`main` workflows, so `main` deploys. A merge by `GITHUB_TOKEN` would not.

Auto-merge removes only the click after approval.
The `main` ruleset still requires the code-owner approval, approval of the last push and green required checks.

as-agent reads the PR URL from the output of `gh pr create`.
It captures that output and prints it again, so `gh` cannot prompt: pass `--title` and `--body` (or `--body-file`, or `--fill`).

as-agent skips auto-merge and prints why in these cases:

- `AGENT_NO_AUTO_MERGE=1` is set: the opt-out for one PR.
- `--draft` or `-d`: GitHub does not auto-merge a draft.
- `--base` names a branch other than `main`: a stacked PR. Only `main` has the review ruleset.
  When GitHub retargets the PR to `main` after its parent merges, enable auto-merge by hand with the command below.
- `--web` or `--dry-run`: gh opens no PR from the command line.

```sh
# Open one PR without auto-merge.
AGENT_NO_AUTO_MERGE=1 factory/bin/as-agent gh pr create --title "CHB-1: title" --body-file body.md
# Turn auto-merge off on an open PR. The owner can also click "Disable auto-merge".
factory/bin/as-agent gh pr merge --disable-auto 61
# Turn it on by hand, for example on a stacked PR retargeted to main.
factory/bin/as-agent gh pr merge --auto --squash 61
```

If the PR opens but auto-merge fails, as-agent exits 5 and prints the command to retry.
The PR is open: do not create it again.

## Subagent models

The owner's Claude Code sets `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` (2026-11-07), so subagents run on Sonnet by default.
An agent that implements a CHB ticket, or does design or review work, runs on Opus: pass `model: "opus"` when spawning it.
Lookups and mechanical briefs stay on the default.

## Acting in Jira as the agent

`factory/bin/jira` calls the Jira REST API of `islomar.atlassian.net` as the Claude account ("Claude (CHB agent)"), CHB-107.
Its email and API token are read from the 1Password item `chb-jira-claude.api-token` (fields `username` and `credential`, vault `chb-factory`) on every call, with one `op` call as the service account.
The Jira token stays inside the Python process: never printed, written to a file or passed to another command.
The owner's `acli` login is global state and is not touched.

```sh
factory/bin/jira view CHB-12
factory/bin/jira search 'status = "In Review"'   # sent as project = CHB AND (<jql>)
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
A file that is not valid UTF-8, an empty summary or an empty comment body exits 2 before the wrapper reads 1Password.

`search` reads only CHB. It sends `project = CHB AND (<jql>)`, with a trailing `ORDER BY` kept outside the parentheses.
JQL with a `)` before its `(`, a `(` never closed, an unterminated quote, a backslash outside quotes or a parenthesis after `ORDER BY` exits 2 before any request.
An answer that lists an issue outside CHB exits 1 and prints nothing.

`rank` calls `PUT /rest/agile/1.0/issue/rank`. When Jira ranks only some of the issues (HTTP 207), it exits 1 and names each issue that failed.

`section` reads the description and finds the top-level heading with that exact text.
It replaces the section up to the next heading of the same or a higher level, or appends the section when the heading is absent.
The new section is a level-2 heading and the body. In the body, lines that start with a dash and a space become a bullet list.
It sends every other part of the description back unchanged. It refuses a description with two such headings.
It reads, then writes: it overwrites an edit that someone else makes between the two requests.
In a section headed "Acceptance criteria (Validating)", each "When:" line needs a "Speed up:" line before the next "When:" line (CHB-133).
A bad heading, an empty body, a "When:" line without "Speed up:", a duplicate key or more than 50 issues exit 2 before the wrapper reads 1Password.

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
- the move starts work: the target is In Progress and the ticket comes from a column outside started work (To Do, Done, Discarded).
  It is blocked while In Progress, In Review or Validating has a max on the board and holds that many other CHB tickets or more (CHB-127).
  The message names every full column, its count, its limit and its tickets;
- the target is Needs human and that column has a max and already holds that many other CHB tickets;
- the move starts work from To Do, passes the limits, and the ticket is not the top ticket of To Do, or is the top ticket without acceptance criteria (CHB-125).
  The top ticket and acceptance criteria are defined in `docs/process/kanban.md`, To Do.
  One search, `statusCategory != Done ORDER BY Rank ASC`, gives the rank order and the open tickets.
  The top ticket is the first non-epic ticket in To Do with no open ticket on a "Depends on" line. Missing acceptance criteria do not skip it.
  The ticket's own dependencies and criteria come from its description as just read.
  The message names the open dependencies (or says the ticket is an epic), names the top ticket and the `rank` command that puts the ticket first,
  or gives the `section` command that writes the missing "Acceptance criteria".
  A ticket missing from that search has no known rank and is refused too: try again in a minute.

The limits come from `GET /rest/agile/1.0/board/2/configuration` on every call, never from code.
The count is a JQL search on the column's status ids in project CHB, without the moved ticket.
No other move is limited. Moves into In Review and Validating record a PR or a merge, so a column can go over its max; Jira colours it.
A move back into In Progress from In Review, Validating or Needs human is not new work and is not limited.
A failed board read or open-ticket search exits 1 and does not move the ticket.
After a move out of a column that leaves it below its board min, `transition` prints `In Progress is below its minimum (0/1)` and exits 0.
After every move between columns it prints the free In Progress slots and the next top tickets, one per free slot:
`In Progress 2/3: 1 slot free; next: CHB-123`, `next: CHB-122 (needs acceptance criteria)`, or `next: nothing to start in To Do`.
It prints no such line when In Progress has no max, or when In Progress, In Review or Validating is at its max.
The moved ticket is counted in its new column, never in its old one, and is never "next".
If a count fails, `transition` prints a notice instead and still exits 0: the ticket has moved.
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

## Hook: no personal agent memory

`.claude/settings.json` runs `factory/hooks/deny_agent_memory_writes.py` before every Write, Edit, MultiEdit and NotebookEdit call (CHB-136).
On 2026-11-07 a session saved a work-in-progress snapshot to Claude Code's per-user memory. It went stale within a day, and a later session read it.

The hook denies any path under `.claude/projects/<project>/memory/`. Work state goes in Jira with `factory/bin/jira`. Learnings go in `docs/knowledge/`, a hook or a check (ADR-eedb).
Writes through Bash (`>`, `cp`, `sed -i`) are not covered: the hook stops accidents, not a deliberate bypass.

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

After a valid model review, the App also posts the summary as a PR comment (CHB-141).
The comment asks the owner to react 👍 if the verdict is right and 👎 if not, and to reply with what it missed.
`make review-agreement` counts those reactions on merged PRs and prints the stage 1 exit of ADR-ea8c:

```sh
make -C ~/workspace/corporate-hotel-booking-software-factory review-agreement
```

| File | Role |
|---|---|
| `review/prompt.md` | Review instructions. PR text and code are untrusted data. |
| `review/schema.json` | Output shape: five dimensions, each with a score and findings, plus a summary. |
| `review/gate.py` | Validates the output, recomputes the scores, decides, and builds the check run. |
| `review/resolve_pr.py` | Picks the PR to review from API data: open, not draft, into `main`, same repository, not Dependabot, head equal to the run's SHA. |
| `review/plan.py` | Skip, carry forward or review, and which model. Holds the docs allowlist. |
| `review/agreement.py` | Counts the owner's 👍 and 👎 on review comments of merged PRs. Prints the stage 1 exit. |

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
