<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
## Contents

- [Kanban statuses](#kanban-statuses)
  - [WIP limits](#wip-limits)
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
Priorities are defined in `priorities.md`. The rank in To Do decides the order of work.

## WIP limits

The CHB board (board 2) holds the WIP limits: a min and a max per column, set by the owner in the board settings.
`factory/bin/jira transition` reads them from the board configuration on every move. The code holds no limit.

A WIP limit stops pulling new work. It never stops the board from recording what already happened (CHB-127).

- Max, starting work: a move from To Do into In Progress exits 4 while In Progress, In Review or Validating holds its max or more.
  The message names every full column, the limit and the tickets in it.
  The ticket stays in To Do. The agent comments on it, tells the owner and starts nothing else.
- Max, Needs human: a move into Needs human exits 4 while that column is full. The ticket stays where it is; the agent comments and tells the owner.
- Moves into In Review and Validating are never blocked: they record a PR opened or a PR merged. A column can go over its max, and Jira colours it.
- A move back into In Progress (review asked for changes, a live check failed, the owner answered) is not new work and is never blocked.
- Moves within one column, out of a column, or into a column without a max are never blocked.
- The max values live on the board only. This document does not repeat them.
- Min: a signal only. When a move leaves a column below its min, the wrapper prints `<column> is below its minimum (count/min)`,
  for example `In Progress is below its minimum (0/1)`.
- Free slots: after every move between columns, the wrapper prints the free In Progress slots and the next top tickets of To Do, one per free slot,
  for example `In Progress 2/3: 1 slot free; next: CHB-123` (CHB-125).
  A ticket without acceptance criteria is marked: `next: CHB-122 (needs acceptance criteria)`.
  It prints no such line while In Progress, In Review or Validating is at its max: starting is blocked then.
  The moved ticket is never next, and it blocks its dependents unless it moved to Done or Discarded (CHB-143).
- Pulling work: the agent fills In Progress up to its max, one agent per ticket, in parallel where it can (owner rule, 2026-11-10).
  It pulls the top tickets of To Do in rank order and needs no go per ticket. The owner can say stop at any time.
  Finishing comes first: before it pulls a ticket, the agent drives what it can in Validating, In Review and In Progress (live checks, review fixes, owner steps).
- Owner override: the owner moves the ticket in the Jira UI and writes why in a comment on the ticket.

Jira Free only colours a column over its limit. The check lives in `factory/bin/jira`.
A Claude Code hook in `.claude/settings.json` denies raw `acli` writes and direct REST writes, so agents cannot bypass the check.

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
- Top ticket: an agent picks the top ticket of To Do: the highest-ranked ticket that is not an epic and has no open ticket on a "Depends on" line.
  To start another ticket, rank it to the top first, with the owner's go.
- Dependencies: a "Depends on" line counts the CHB keys in its first sentence,
  and the list under it when the line ends with ":". Open means a status outside the Done category; Done and Discarded are closed.
  Write every dependency on such a line, also an owner decision: make it a ticket and name it (for example `Depends on: CHB-141`).
  A dependency in prose is not checked.
- Acceptance criteria: a heading that starts with "Acceptance criteria", or a line that holds only that label (any case, bold and a colon allowed),
  with text under it.
  "Acceptance criteria (Validating)" does not count. A top ticket without them is not skipped: the agent writes them
  (`factory/bin/jira section CHB-<n> --heading "Acceptance criteria" --body-file FILE`), derived from the ticket, its epic and the ADRs.
  It asks the owner only for decisions that are the owner's (owner rule, 2026-11-10).
- `factory/bin/jira transition <key> "In Progress"` from To Do exits 4 when `<key>` is not that top ticket, has an open dependency,
  or is the top ticket without acceptance criteria (CHB-125). The message names the top ticket and the next step.
  A move back into In Progress from another status is not checked.
- WIP limit: none.

### In Progress

Meaning: someone (the agent or the owner) is changing code, infrastructure or documents for this ticket.

- Enter: the worker assigns the ticket to themselves (an agent runs `factory/bin/jira assign CHB-<n> --me`) and creates the branch `CHB-<n>-<slug>` from `main`.
- Leave to In Review: a pull request is open, `make check` passed locally, and the PR body states what merging changes in plain words.
- Leave to Needs human: the worker is blocked by a question or a decision only the owner can make.
- WIP limit: a min and a max on the board. The max is the most agents that run in parallel. See [WIP limits](#wip-limits).

### In Review

Meaning: a pull request waits for review and merge.

- Enter: the PR is open and linked from the ticket.
- Leave to Validating: the PR is merged into `main`. CI passed and the required reviews were given.
- Leave to In Progress: review asked for changes.
- WIP limit: a max on the board. A full In Review blocks starting work, never opening the PR of work already started.
  The agent then reviews, fixes or closes pull requests before it starts another ticket.

### Validating

Meaning: the change is merged and deployed, but nobody has yet confirmed that it works in the running system. Merged is not done.

- Enter: the PR is merged and, where it applies, deployed or applied.
  For Terraform changes, the owner has run `make infra-apply`.
- Enter: in the same step, the worker writes a section headed "Acceptance criteria (Validating)" in the ticket description, not in a comment.
  An agent writes it with `factory/bin/jira section CHB-<n> --heading "Acceptance criteria (Validating)" --body-file FILE`.
  A reader without context must see what is still missing. For each criterion not yet checked live, the section states:
  - What must happen, in the live system.
  - When: an absolute date and time, or the event that triggers it (for example "the next Dependabot run, 2026-11-11 06:00 Europe/Madrid").
  - Speed up: a line of its own right after When. It names the exact step that lets the owner trigger it earlier, or says "cannot be sped up" and why.
    Examples: Dependabot "Check for updates" in the repository's Insights, Dependency graph, Dependabot tab.
    "Run workflow" on the Actions page of a workflow that declares `workflow_dispatch`. "Re-run jobs" on a failed run.
    `factory/bin/jira section` refuses the section (exit 2) while a "When:" line has no "Speed up:" line.
  - Where to check: the exact URL or command.
  - Pass condition: what the check must show.
  - Who checks: the agent or the owner. If the owner must act, the section has numbered steps, as for Needs human.
  - If it fails: what happens next, and by when the ticket goes back to In Progress.
- `factory/bin/jira transition` refuses the move (exit 4) while that section is missing.
- Leave to Done: each acceptance criterion is checked in the live system, and the evidence is written as a ticket comment.
  Evidence is concrete: the smoke test of the deploy, a `gcloud` or `gh api` read of the real setting, a screenshot, a log line, a metric.
  A passing plan or a green CI run is not evidence of the live result.
- Leave to In Progress: the check fails. A new or reopened PR fixes it.
- Some checks need time (for example a background cleanup that runs within a day). The ticket stays in Validating until then.
- WIP limit: a max on the board. A full Validating blocks starting work, never recording a merge.
  The agent then checks live results or tells the owner how to speed them up before it starts another ticket.

### Needs human

Meaning: the work cannot continue without the owner.

- Enter: an open question, a decision, an action only the owner can do (a click in a web UI, a sudo command, a payment setting), or a gate that failed and needs judgement.
  The worker writes a section headed "Needs human: owner steps" in the ticket description, not in a comment, with `factory/bin/jira section`.
  It is a numbered step-by-step procedure: what to do, where (exact URL or menu path), how (each click or command).
  It also states what not to touch, the deadline if any, and how the owner reports that it is done.
  `factory/bin/jira transition` refuses the move (exit 4) while that section is missing.
- Leave: the owner answers or acts. The ticket goes back to the status it came from.
- This status is the top of every notification.
- WIP limit: a max on the board, a hard block on entering. When the column is full, the agent leaves the ticket in its status, comments on it, tells the owner and starts nothing else.

### Done

Meaning: the acceptance criteria hold in the live system, with evidence on the ticket. Nothing more is expected.

- Enter: only from Validating, or directly for tickets with nothing to deploy (a decision, an investigation) once their output exists: a merged ADR, a ticket comment with findings.

### Discarded

Meaning: the ticket will not be done. This is a valid outcome.

- Enter: from any status. A comment states why: superseded, wrong idea, no longer needed.

## Assignee

The person or agent doing the work is the assignee.
Agent work is assigned to the Claude account ("Claude (CHB agent)").

Agents make every Jira write (create, transition, comment, edit-comment, assign, rank, section, description, priority) through `factory/bin/jira`, so Jira history shows the Claude account as the author.
`acli` is logged in as the owner and is for the owner only.
When `factory/bin/jira` fails, the agent stops and reports. It never falls back to `acli`.
A Claude Code hook denies `acli` writes and direct REST writes to Jira in this repository.
See `factory/README.md`.
