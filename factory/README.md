<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Factory](#factory)
  - [Credentials: the 1Password service account](#credentials-the-1password-service-account)
    - [Rotating the token](#rotating-the-token)
  - [Acting as the agent](#acting-as-the-agent)
    - [Auto-merge on agent PRs](#auto-merge-on-agent-prs)
    - [Keeping agent PRs up to date](#keeping-agent-prs-up-to-date)
  - [Subagent models](#subagent-models)
  - [Acting in Jira as the agent](#acting-in-jira-as-the-agent)
    - [Board rules on transition](#board-rules-on-transition)
    - [Hook against raw Jira writes](#hook-against-raw-jira-writes)
  - [Hook: commands for the owner work from any folder](#hook-commands-for-the-owner-work-from-any-folder)
  - [Hook: act on the WIP minimums](#hook-act-on-the-wip-minimums)
  - [Hook: no personal agent memory](#hook-no-personal-agent-memory)
  - [Hook: branch names follow the ticket key](#hook-branch-names-follow-the-ticket-key)
  - [Hook: related tests only while iterating](#hook-related-tests-only-while-iterating)
  - [Hook: warn on repeated edits of one file](#hook-warn-on-repeated-edits-of-one-file)
  - [Hook: no harness edits in feature tickets](#hook-no-harness-edits-in-feature-tickets)
  - [Hook: no secrets in the agent's context](#hook-no-secrets-in-the-agents-context)
  - [Hook: what to finish first, at session start](#hook-what-to-finish-first-at-session-start)
  - [Automated PR review (shadow mode)](#automated-pr-review-shadow-mode)
  - [Workflow output schemas and routing](#workflow-output-schemas-and-routing)

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

as-agent warns on stderr when a `-F`, `--file` or `--body-file` path lies in a Claude Code `scratchpad` folder without a `CHB-<n>` folder, for example `<scratchpad>/pr.md`.
Parallel agents that share one scratch folder overwrite each other's files (CHB-138). The warning never blocks the command.
`factory/bin/jira` does not check its `--body-file` paths.

### Keeping agent PRs up to date

`.github/workflows/pr-updates.yml` runs on every push to `main` and on manual dispatch, as the agent App (CHB-172).
Why: an owner "Update branch" click makes the owner the last pusher, and the ruleset's `require_last_push_approval` then ignores the owner's approval.
`docs/knowledge/conflicting-agent-prs-need-the-agent-not-the-owner.md` has the two cases that led to it.

`factory/pr_updates.py` reads the open PRs and acts only on PRs opened by the App (a `Bot` author named `chb-factory-agent`), into `main`, from this repository:

| PR state | Action |
| --- | --- |
| Behind `main`, mergeable, not approved | `updatePullRequestBranch` with `updateMethod: REBASE` and `expectedHeadOid` set to the head it read. A push in between makes GitHub refuse; the run logs "head moved" and goes on. |
| Behind `main`, mergeable, approved | Nothing. `strict_required_status_checks_policy` is false, so it can merge, and a rebase would dismiss the approval. |
| Conflicting | Adds the label `needs-rebase`. No update. |
| Labelled `needs-rebase`, mergeable again | Removes the label. |
| Mergeable `UNKNOWN` | Read again up to 3 times, 5 s apart; then left alone until the next push. |

The SessionStart sweep lists open agent PRs labelled `needs-rebase` as "resolve first".
The agent rebases them locally, pushes through `factory/bin/as-agent` and removes the label.
Tests: `factory/tests/test_pr_updates.py`, `factory/tests/test_validating_sweep.py`.

The App key reaches the job only through the `agent-pr-updates` environment, which admits only `main` (ADR-d7b7, `infra/github/environments.tf`).
Secrets there: `AGENT_APP_CLIENT_ID` and `AGENT_APP_PRIVATE_KEY`. Until both exist the job prints a notice and does nothing.
The token asks for `contents`, `pull-requests` and `workflows` write. `workflows` is needed to rebase a PR that edits a workflow file.
The label `needs-rebase` is a Terraform resource in `infra/github/labels.tf`.

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
factory/bin/jira tick CHB-12 small   # one box of the Definition of Ready: why, trigger, decision or small
factory/bin/jira ready CHB-12 --trigger "2. the owner asked for it on 2026-11-10"   # Backlog to Ready
factory/bin/jira checklist CHB-12   # or --open: adds the Definition of Ready where it is missing
factory/bin/jira comment CHB-12 --body-file comment.txt   # "-" reads stdin
factory/bin/jira assign CHB-12 --me
factory/bin/jira rank CHB-12 CHB-14 --before CHB-9   # or --after; up to 50 issues, kept in that order
factory/bin/jira section CHB-12 --heading "Acceptance criteria (Validating)" --body-file section.txt
factory/bin/jira section CHB-12 --heading "Why it matters" --first --body-file why.md   # at the top
factory/bin/jira sort-todo   # ready High, Medium, Low, then blocked, then epics
factory/bin/jira create --parent CHB-27 --summary "Bookings fail" --description-file why.md --expedite "production bug: HTTP 500 on every booking since 10:02"
factory/bin/jira priority CHB-12 High   # High, Medium or Low (docs/process/priorities.md)
factory/bin/jira description CHB-12 --file description.md   # replaces the whole description
factory/bin/jira edit-comment CHB-12 10071 --body-file comment.md   # the agent's own comments only
factory/bin/jira link CHB-88 --blocked-by CHB-146   # a "Blocks" link: CHB-146 blocks CHB-88
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
With `--first` the section goes at the top instead, and an old copy of it is removed wherever it was.
The heading "Why it matters" always goes first, with or without `--first` (CHB-148).
In a section headed "Acceptance criteria (Validating)", each "When:" line needs a "Speed up:" line before the next "When:" line (CHB-133).

`create` refuses a description that does not open with a non-empty "Why it matters" section (exit 2, before 1Password and any request, CHB-148).
The first node must be a heading with exactly that text, and text must follow before the next heading of the same or a higher level.

The pull list is the board column "Ready", or "To Do" on a board without that column (`pull_list`, CHB-153). The board was renamed on 2026-11-10.
The "Ready" column holds two statuses, "To Do" (10003) and "Ready" (10010); "Backlog" holds status 10009.
Backlog is never sorted, never "next" and never the top ticket. A ticket leaves Backlog only for the pull list, Done or Discarded: any other move exits 4.
`transition` takes a status name or, when no status has that name, a column name.
`create` puts a new ticket in Backlog when the board has that column: it reads the new ticket's status and moves it only when Jira put it elsewhere.
Every failure after the ticket exists is a notice with exit 0, so nobody creates it twice.

Definition of Ready (CHB-154). Every ticket has a section headed "Definition of Ready" with six Jira checkboxes (an ADF `taskList`), right after "Why it matters".
The heading takes the level of the "Why it matters" heading. Without that section the checklist goes first.
`create` adds it. `checklist CHB-<n> ...` or `checklist --open` adds it to tickets without it, from a fresh read of each ticket, and changes no other node.
It skips epics, prints one line per key and a count, and leaves a ticket with two such sections alone (exit 1 at the end).
`tick CHB-<n> <box>` ticks one judgment box: `why`, `trigger`, `decision` or `small`. It changes only that box's `state`; ids and text stay.
`acceptance` and `unblocked` are verified boxes: `tick` refuses them (exit 2). A box is found by the start of its text (`Box.prefix` in `jira.py`).
`ready CHB-<n> --trigger "<which trigger and its evidence>"` is the only way from Backlog to the pull list.
Trigger 5 (the text starts with `5`) is accepted only while Ready is below its board minimum; otherwise `ready` exits 4 and writes nothing (CHB-164).
It refuses (exit 4, naming each open box) a missing checklist, an unticked judgment box, a description without a "Why it matters" first section,
no acceptance criteria, or an open blocker. A ticked box does not replace its check. On a ticket outside Backlog it exits 2.
When every box passes, it ticks the verified boxes, moves the ticket and comments `Moved to Ready. Trigger: <text>`. A failed comment exits 1 and says the ticket moved.
`transition` refuses a move from Backlog to the pull list, and any `--trigger` (exit 2, before any request); both messages name `ready`.
`create --ready` and `create --trigger` exit 2 before any request: tick the boxes, then run `ready`.

Expedite (CHB-157). `create --expedite "<case>: <evidence>"` creates the ticket with priority Highest and moves it straight to In Progress.
The case is one of `production bug`, `pipeline broken` or `secret leak`; any other reason, or `--priority` next to `--expedite`, exits 2 before 1Password.
It reads the open tickets first and exits 4, with no write, while another ticket with priority Highest is open.
The move skips `before_transition`: it passes Backlog and Ready and every column limit, so a full column goes over its max by one.
Then it comments `Expedite: <reason>`, assigns the ticket to the agent, and prints `CHB-<n> -> In Progress (expedite). Tell the owner now: <reason>`.
A failed move after the create is a notice with exit 0: the owner moves the ticket in the Jira UI.
A failed comment or assign after the move is a notice that names the command to run; the ticket is already in the In Progress column.
After the move it reads the open tickets again and prints `more than one open expedite ticket: ...` when two agents created one at the same moment.
`priority CHB-<n> Highest` and `create --priority Highest` exit 2 and name `create --expedite`.

`sort-todo` ranks the pull list in the order of `docs/process/kanban.md`, Ready (CHB-148): tickets in `top_queue` (they can start) first, High, then Medium, then Low;
then the other non-epic tickets (blocked by an open ticket); then epics. Each group keeps its rank order. A missing priority counts as Medium.
Readiness comes from `top_queue` only, so a change to the dependency rule changes the sort too.
It sends no request when the pull list is already sorted. Otherwise it ranks the new top ticket before the old one,
then the tickets from the first misplaced one, each chunk of up to 50 after the last key ranked.
`create` runs it after creating a ticket in the pull list, never for a ticket in Backlog.
The search can miss a ticket created a moment ago, so the new ticket is then sorted as the last ticket of the pull list.
A failed sort after `create` prints `could not sort Ready: ...` and exits 0: the ticket exists, so do not create it again; run `sort-todo`.
The rule decides the order, with no owner exception. The owner moves a ticket by changing its priority, or by ranking it within its priority.
A bad heading, an empty body, a "When:" line without "Speed up:", a duplicate key or more than 50 issues exit 2 before the wrapper reads 1Password.

`view` shows a bullet list as lines that start with a dash, and each issue link as a line such as `Is blocked by: CHB-146 (To Do)` or `Blocks: CHB-88 (To Do)`.

`link CHB-<a> --blocked-by CHB-<b>` creates a Jira "Blocks" link with `POST /rest/api/3/issueLink`.
The POST body names the sides the other way round from a read: the blocker `CHB-<b>` is `inwardIssue` and `CHB-<a>` is `outwardIssue`.
A read of `CHB-<a>` then lists `CHB-<b>` as `inwardIssue` ("is blocked by"). A live check on 2026-11-10 showed it.
It reads the links of `CHB-<a>` first and changes nothing when the link exists.
It reads them again after the POST and exits 1 when `CHB-<b>` does not show as a blocker.
A key outside CHB or a ticket linked to itself exits 2 before the wrapper reads 1Password.
It never removes a link: the owner does that in the Jira UI.

Exit codes: 0 done, 1 Jira refused or failed, 2 usage error, 3 no credential (no service account token in the keyring, or `op` failed), 4 a board rule blocks the transition.
On 3 the agent stops and reports. On 4 the ticket does not move: the agent comments on it and reports.

The account is a CHB project Administrator, not a Member: Jira Free has no Member role.
It can delete issues and change project and board settings through the REST API.
The wrapper is the guardrail:

- it accepts only `CHB-<n>` keys and creates only Tasks under a CHB Epic;
- `rank` and `sort-todo` change only the rank of issues, `section` writes only the description field, and `link` only adds "Blocks" links between two CHB issues;
- it has no delete, no project, board, workflow or permission write, and no raw request command;
- it refuses HTTP redirects, so the token is never sent to another host.

Do not add a generic request command or a delete command.
The Jira token itself still has the Jira account's full rights. Anyone who reads it bypasses the wrapper.
The 1Password service account narrows who can read it to processes that can read the keyring item, but does not narrow what the Jira token can do.

### Board rules on transition

`before_transition` in `jira.py` runs before every transition (CHB-108). It exits 4 (exit 5 for the two full-column cases below) and does not move the ticket when:

- the target status is Validating and the description has no section headed "Acceptance criteria (Validating)" with text under it;
- the target status is Needs human and the description has no section headed "Needs human: owner steps" with text under it;
- the move starts work: the target is In Progress and the ticket comes from a column outside started work (Ready, Done, Discarded). A start from Backlog is refused.
  It is blocked while In Progress, In Review or Validating has a max on the board and holds that many other CHB tickets or more (CHB-127).
  The message names every full column, its count, its limit and its tickets;
- the target is Needs human and that column has a max and already holds that many other CHB tickets;
- the target is the pull list (Ready), the ticket is not an epic, and Ready has a max and already holds that many other tickets (CHB-157).
  This covers `ready` and a move back into Ready. The check runs before `ready` ticks any box.
  The message names the tickets in Ready and the command that moves one back to Backlog to make room;
- the move starts work from the pull list, passes the limits, and the ticket is not the top ticket of the pull list,
  or is the top ticket without a "Why it matters" first section or without acceptance criteria (CHB-125, CHB-148).
  The top ticket and acceptance criteria are defined in `docs/process/kanban.md`, Ready.
  One search, `statusCategory != Done ORDER BY Rank ASC`, gives the rank order and the open tickets.
  The search also returns each ticket's `issuelinks`. A ticket is blocked by every ticket on an "is blocked by" link of type "Blocks" that is still open (`open_blockers`, CHB-151).
  "Depends on" text is not read.
  A blocked ticket, an epic or a ticket missing from the search is refused first, with no write.
  Then the check sorts the pull list (`sort-todo`, a rank write before the move) and takes the top ticket from the sorted order, without a second search.
  The top ticket is the first non-epic ticket in the pull list that no open ticket blocks. Missing "Why it matters" or acceptance criteria do not skip it.
  The ticket's own links and criteria come from the read just made.
  The message names the open blockers (or says the ticket is an epic), names the top ticket and the `priority` and `rank` commands that put the ticket first,
  or gives the `section` command that writes the missing "Why it matters" or "Acceptance criteria".
  A ticket missing from that search has no known rank and is refused too: try again in a minute.

The limits come from `GET /rest/agile/1.0/board/2/configuration` on every call, never from code.
The count is a JQL search on the column's status ids in project CHB, without the moved ticket and without epics (`issuetype != Epic`): the board shows no epic as a card.
No other move is limited. Moves into In Review and Validating record a PR or a merge, so a column can go over its max; Jira colours it.
A move back into In Progress from In Review, Validating or Needs human is not new work and is not limited.
A failed board read or open-ticket search exits 1 and does not move the ticket.
After a move out of a column that leaves it below its board min, `transition` prints `In Progress is below its minimum (0/1)` and exits 0.
Ready is a quota (CHB-164). After any move between columns, while Ready holds fewer than its min, `transition` and `ready` print
`Ready is below its minimum (<count>/<min>): refill it from Backlog now ...`, then one line per refill candidate, and exit 6 after the move.
A candidate is a Backlog ticket that is no epic and has no open blocker: `CHB-30 (High): missing Acceptance criteria`, or `complete`. The list is as long as Ready lacks tickets,
highest priority first, then rank order. Exit 6 means "done, now refill"; it repeats on every later move until Ready is back at its min. A failed read of the count is a notice and exit 0.
After every move between columns it prints the free In Progress slots and the next top tickets, one per free slot:
`In Progress 2/3: 1 slot free; next: CHB-123`, `next: CHB-122 (needs Why it matters and acceptance criteria)`, or `next: nothing to start in Ready`. Backlog tickets are never next.
The next tickets come in the sorted order (`todo_order`), the same order the start check uses.
It prints no such line when In Progress has no max, or when In Progress, In Review or Validating is at its max.
The moved ticket is counted in its new column, never in its old one, and is never "next".
It blocks the pull-list tickets it has "Blocks" links to unless it moved to Done or Discarded, whatever the search still shows (CHB-143).
The start check and this line take the top tickets from one function, `top_queue`, through `todo_order`.
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

## Hook: act on the WIP minimums

`.claude/settings.json` runs `factory/hooks/check_wip_minimums.py` when the agent finishes a reply (Stop hook, CHB-162).
On 2026-11-10 the agent ended its turn four times with the wrapper's "below its minimum" line on screen, and the owner had to push it.

The hook reads the column minimums from the board and the open tickets as the agent account (one 1Password read, two Jira reads). It exits 2 with a reason when:

- In Progress is below its minimum and Ready has a ticket that can start. The reason names the top ticket and the `transition` command.
  It stays quiet while In Progress, In Review or Validating is at its max.
- Ready is below its minimum (CHB-164). No reply text passes this; the reason lists the refill candidates, as `transition` does.
- A Validating ticket has a due item and the agent account has no comment on it from the current day (CHB-163). "Due" is as `validating_sweep.py` defines it
  (CHB-140): the earliest absolute date of a "When:" line has passed. The day is the Europe/Madrid day. The reason names each ticket and each due item.
  Items not yet due, "When:" lines without a date and tickets without the section never block. Comments are read only for tickets with a due item, through the same Jira client.
  A failed comment read prints one line and skips this check; the WIP checks still run. To pass: check the item in the live system and comment the evidence with `factory/bin/jira comment`.

It fails open with one line on stderr when Jira or 1Password cannot be reached. It refuses once per session for the same findings: the second stop with the same findings passes
(state in `<temp dir>/chb-wip-minimums-<uid>/`, folder override `CHB_WIP_MINIMUMS_STATE_DIR`). Like the other hooks it is a guardrail: it reads the board, not the reply text (CHB-164).

`factory/bin/jira transition` exits 5 for a WIP limit (a full In Progress, In Review, Validating or Needs human) and 4 for every other board refusal. The exit 5 message names a wait command:
a bash loop that retries the move every 120 seconds while the exit code is 5. It ends at once on any other code (0 moved; 4 not the top ticket, a missing section or a blocked ticket, printed),
and after 60 minutes it prints that the slot is still full and exits 1. The agent runs it in the background.
`transition` and `ready` exit 6 when the move is done and Ready is below its minimum (CHB-164). It is not a refusal: nothing waits for it, and the wait command passes it on as the exit of a finished move.

## Hook: no personal agent memory

`.claude/settings.json` runs `factory/hooks/deny_agent_memory_writes.py` before every Write, Edit, MultiEdit and NotebookEdit call (CHB-136).
On 2026-11-07 a session saved a work-in-progress snapshot to Claude Code's per-user memory. It went stale within a day, and a later session read it.

The hook denies any path under `.claude/projects/<project>/memory/`. Work state goes in Jira with `factory/bin/jira`. Learnings go in `docs/knowledge/`, a hook or a check (ADR-eedb).
Writes through Bash (`>`, `cp`, `sed -i`) are not covered: the hook stops accidents, not a deliberate bypass.

## Hook: branch names follow the ticket key

`.claude/settings.json` runs `factory/hooks/deny_bad_branch_names.py` before every Bash command (CHB-54).
One Jira ticket per branch, named `CHB-<n>-<slug>`: `^CHB-[0-9]+-[a-z0-9]+(-[a-z0-9]+)*$`.

The hook exits 2 when one of these commands names a branch that does not match:

- `git switch -c`, `-C`, `--create`, `--force-create` or `--orphan`;
- `git checkout -b`, `-B` or `--orphan`, and `git worktree add -b` or `-B`;
- `git branch <name>`, and the new name of `git branch -m`, `-M`, `-c` or `-C`;
- `git push <remote> <name>`, `<src>:<name>` or `refs/heads/<name>`.

These pass: `main`, `dependabot/...`, Claude Code's worktree branches `worktree-agent-...`, `git branch` listing and deleting, `git push` with no refspec, of `HEAD`, of a tag, or a deletion.
A name built from `$VAR` or `$(...)` passes: the hook cannot read it.
The hook denies a plain `git push origin <tag>`. Push the tag as `refs/tags/<tag>`.

## Hook: related tests only while iterating

`.claude/settings.json` runs `factory/hooks/deny_full_test_suite.py` before every Bash command (CHB-54).
It denies a bare full-suite run and names the related-test form:

- `pytest`, `python -m pytest` or `uv run pytest` without a path (a word with `/` or `::`, or ending in `.py`), `-k` or `--lf`. A marker (`-m slow`) does not narrow the run;
- `vitest`, `npx vitest run`, `npm test` or `npm run test` without a file filter, `-t` or `--changed`;
- `python3 -m unittest` or `python3 -m unittest discover` without `-s <folder>`, `-p <pattern>`, `-k`, a module or a file.

A path of `.` counts as no filter. A number does not count as a vitest filter: it is likely the value of an option such as `--retry 2`.
`make check` and `make test` run the full suite and stay allowed: CLAUDE.md requires `make check` before every commit.

The two Bash hooks drop redirections (`2>&1`, `> file`, `2>/dev/null`) and strip `rtk`, `rtk proxy`, `factory/bin/as-agent`, `env`, `uv run`, `npx` and `python -m` before they read a command.
Like the Jira hook, they read the command text only, deny on unreadable input or an internal error, and let the command run if `python3` cannot start.
They split a command with unbalanced quotes, such as a heredoc with an apostrophe, on spaces and separators instead.

## Hook: warn on repeated edits of one file

`.claude/settings.json` runs `factory/hooks/warn_edit_loops.py` after every Write, Edit and MultiEdit call (PostToolUse, CHB-54).
It counts the edits of each file per session. On the 5th and 8th edit it adds context for the agent:
"5th edit of <file> this session: step back, re-read the failing test and the plan before editing again."

The counts live in `<temp dir>/chb-edit-loops-<uid>/<session id>.json`. The env var `CHB_EDIT_LOOP_STATE_DIR` overrides the folder.
The hook refuses a folder that is a symlink, belongs to another user or is open to others, and opens the state file without following symlinks.
The hook never blocks. Bad input, an unreadable state file or an internal error gives no warning and exit 0.
`make clean-dev` runs `warn_edit_loops.py --prune`: it removes state files older than 7 days (CHB-138).
The temp folder follows `TMPDIR`, so a session with another `TMPDIR` keeps its state elsewhere. Run the prune with the same `TMPDIR`.

## Hook: no harness edits in feature tickets

`.claude/settings.json` runs `factory/hooks/deny_harness_edits.py` before every Write, Edit, MultiEdit, NotebookEdit and Bash call (CHB-30, Bash in CHB-165).
CLAUDE.md: do not edit `.claude/`, `factory/` or `.github/` as part of a feature ticket.

The hook denies the edit when all of these hold:

- the path's first folder, relative to the top of its own git worktree, is `.claude`, `factory` or `.github`;
- that worktree's branch is `CHB-<n>-...`;
- CHB-<n> is a product epic, or its Jira parent is one. The product epics are CHB-4, CHB-14, CHB-19 and CHB-50 (`PRODUCT_EPICS` in the hook, set by the owner).

These pass: tickets of other epics, tickets without a parent, `main`, `worktree-agent-...` and any other branch (the branch name hook guards names), and paths outside a git repository.
A file under `<repo>/.claude/worktrees/agent-x/` belongs to that worktree, so `backend/` files there pass.
The hook resolves symlinks first, also the file's own, so a link into a harness folder counts as the harness path.

The hook calls `factory/bin/jira view CHB-<n>` only for a harness path, with a 20-second timeout.
It caches the parent per branch in `<temp dir>/chb-harness-epics-<uid>/<branch>.json`. The env var `CHB_HARNESS_EPIC_CACHE_DIR` overrides the folder.
A folder that is a symlink, belongs to another user or is open to others is not used. Cache files open without following symlinks.
The cache never expires. If a ticket moves to another epic, delete its branch file.
If the Jira read fails (no token, timeout, HTTP error), the hook denies the edit and names `factory/bin/jira view CHB-<n>`. A failure is not cached.
If `git` times out (10 seconds) or does not start, the hook denies an edit of a harness path and says so (CHB-152). Other paths never call `git`.
A Bash command is parsed by `factory/hooks/harness_bash_writes.py` for the paths it may write, and each path gets the verdict of an Edit of that path (same reason text).
Read:

- output redirections (`>`, `>>`, `>|`, `&>`, `2>`), `tee`, `sed -i` and `perl -i`;
- the destination of `cp`, `install`, `ln` and `rsync`, and every operand of `mv`, `rm`, `rmdir`, `touch`, `mkdir`, `truncate` and `dd of=`;
- `cd` before any of them, `bash -c '...'` and shell here-documents;
- python, node, ruby, perl or php code that calls a write function and names a harness path.

A command that only reads a harness path passes: `cat`, `grep`, `git diff`, `git show`, `cp` from it. The body of a here-document read by another program (`cat > f <<EOF`) is data.
A shell whose working directory is inside a harness folder is checked for every relative write.
The hook cannot catch:

- paths built from variables or command substitution;
- scripts and tools that write on their own (`python3 script.py`, `make`, `curl -o`);
- `git checkout`, `git apply` and `git restore`;
- `eval`, `xargs` and `find -exec`;
- interpreter code that builds a path from pieces.

`docs/knowledge/harness-guard-bash-limits.md` lists them. Code-owner review of the harness folders is the backstop.
The agent's user can write the cache folder, so a planted cache file also gets past the hook.
The hook stops accidents, not a deliberate bypass.

## Hook: no secrets in the agent's context

`.claude/settings.json` runs `factory/hooks/deny_secret_reads.py` before every Read and Bash call (CHB-30).
`factory/bin/as-agent` and `factory/bin/jira` read their token inside their own process, so the agent never needs a secret. `.env.example` lists the variable names.

Secret paths: a path with a `.ssh` folder (`~/.ssh/**`), `*.pem`, `*.key`, `.env` and `.env.*` except `.env.example`, and a path with `.config/op` (`~/.config/op/**`).
`~`, `$HOME` and `${HOME}` expand. A glob that can match one counts: `*.pem`, `x.k*`, `.env*`, `.e*`, `~/.ss*/id_rsa`.
A glob that does not start with `.` never matches `.ssh` or `.env`, so `grep -rn TODO *` passes. `*.*` passes too.
`.envrc`, a bare `.key` and a hidden `.x.key` do not count: they read like `jq` filters such as `.foo.key`.

The hook exits 2 for:

- Read of a secret path, or of a symlink whose real path is one; a relative path resolves against the session folder;
- a Bash command with a secret path as an argument, as an `--option=<path>` value, or after a `:` (`git show HEAD:.env`, `scp host:~/.ssh/id`);
- input redirected from a secret path (`< .env`), whatever the program;
- `secret-tool lookup` and `op read` run directly.

The hook also checks the body of every `$(...)` and backtick substitution, quoted or not and with nested parentheses (`"$( (cat .env) )"`).
It checks the arguments of `bash -c`, `sh -c` and `eval`, and the remote command of `ssh`.

These pass even with a secret path: `ls`, `stat`, `test`, `[`, `chmod`, `chown`, `mkdir`, `touch`, `rm`, `echo`, `printf` and `ssh-add`. They do not print the file.
`ssh` and `scp` may name a key or config file with `-i`, `-F` or `-o IdentityFile=`.
`factory/bin/as-agent`, `factory/bin/jira` and `make` are not blocked.

Known false denials: `grep .env docs/` (the pattern reads as a path) and a single-quoted `'$(cat .env)'`.
A heredoc line that names a secret path, such as a line of a commit message, is denied too. Write such a message to a file with the Write tool.
Known gaps: a path built at run time, `cd ~/.ssh` followed by a relative name,
a symlink to a secret file named in a Bash command (Read resolves symlinks, Bash reads the text only), a script that reads the file,
a hidden key file such as `.tls.key`, the Grep tool, and `op` subcommands other than `read`.
Like the other Bash hooks, it reads the command text only, denies on unreadable input or an internal error, and lets the call run if `python3` cannot start.

## Hook: what to finish first, at session start

`.claude/settings.json` runs `factory/hooks/validating_sweep.py` on every SessionStart event: start, resume, `/clear`, compaction and fork (CHB-140).
On 2026-11-10 a 05:15 UTC Dependabot run unblocked two Validating tickets. The next session did not look at them until the owner asked.

The hook adds this to the agent's context before the first prompt:

- Each Validating ticket with the "When:" lines of its "Acceptance criteria (Validating)" section.
  `due`: the earliest absolute date on the line has passed. A date only counts from the start of that day. A time without a zone is Europe/Madrid.
  No mark: the date is still ahead. The date wins over an event named on the same line.
  `check`: the line has no absolute date, so it names an event.
  `no section`: the ticket has no such section.
- Open agent PRs labelled `needs-rebase`, as "resolve first" (CHB-172).
- The line of `make review-agreement`, and up to 5 merged PRs whose last review comment has no owner reaction, newest first, with links.

It reads only: one Jira search as the agent account (credentials from 1Password, as `factory/bin/jira`) and two GitHub GraphQL queries with the owner's `gh` login.
The review agreement query reads up to 3 pages of 100 PRs. The `needs-rebase` query reads one page.
Each call has a 6 s timeout, the three parts run in parallel, and the hook stops waiting after 9 s. Claude Code kills it at 15 s.
It fails open: a part that fails prints one line, `Validating sweep failed: <reason>`, `Review agreement failed: <reason>` or `Needs-rebase list failed: <reason>`, and the session starts.
A reason is the message of a known error, which holds no secret, or only the type name of an unexpected one.
Ticket summaries, "When:" lines and PR titles are quoted on one line of at most 200 characters, without control characters, under a header that marks them as data, not instructions.

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

## Workflow output schemas and routing

No workflow runs on this code yet. CHB-44 (ticket-to-PR workflow) will call it. Ticket: CHB-62.

Each stage of the workflow returns typed JSON. `workflow/schemas/<stage>.json` defines it: it lists every field as required and rejects any other field.
`workflow/route.py` has one function, `next_step(stage, output)`. It validates the output with the validator of `review/gate.py` and returns a `Route` with a step and its reasons.

| Stage | Fields | Rule | Step |
|---|---|---|---|
| `analysis` | `planFile`, `acceptanceCriteria[]`, `isVerifiable`, `blockingQuestions[]` | `blockingQuestions` not empty | `stop-and-notify` |
| `implementation` | `typesClean`, `lintClean`, `testsGreen` | any check false | `another-round` |
| `review` | `mergeSafety`, `scores`, `findings[{severity, suggestedFix}]` | `mergeSafety` below 8.5 or a critical finding | `triage` |
| `triage` | `decision` (`fix`, `dismiss`, `doubt`), `fixableByAgent` | `decision` is `doubt` | `pr-caveats` |

Otherwise the step is `next-stage`.
An unknown stage, invalid output or an unreadable schema returns `stop`. `next_step` never raises.

No rule reads `isVerifiable` or `fixableByAgent` yet. CHB-44 decides what they route to.
`next_step` trusts the `mergeSafety` it receives. `review/gate.py` recomputes Merge Safety from the findings, so CHB-44 should pass the gate's value.
