<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Rules health check](#rules-health-check)
  - [When it runs](#when-it-runs)
  - [Steps](#steps)
  - [What counts as a finding](#what-counts-as-a-finding)
  - [Limits](#limits)
  - [What the hook and the script do](#what-the-hook-and-the-script-do)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Rules health check

Rules live in `CLAUDE.md`, `docs/process/`, `docs/decisions/`, `docs/knowledge/` and `factory/README.md`.
They grow daily and nobody reads them as a whole. Duplicates drift apart, old text names things that no longer exist,
and a newer decision can contradict an older one. Agents then follow the wrong text.
This procedure finds those three problems and proposes the fixes as one pull request ([CHB-167](https://islomar.atlassian.net/browse/CHB-167)).

## When it runs

The first session after 7 or more days since the last run.
`factory/hooks/rules_health_due.py` adds the line "Rules health check is due" to the context at session start.
No timer and no headless run exist. Finish the owner's current request first, then follow the steps below.

The last run is the newest Jira ticket whose summary starts with `Rules health check`.
Step 1 creates it, so the hook stops asking as soon as a run starts, even if its PR waits for review.

## Steps

1. Create the ticket: `factory/bin/jira create --parent CHB-27 --summary "Rules health check <YYYY-MM-DD>" --description-file <file> --priority Medium`.
   The description opens with "Why it matters" and lists the findings F1..Fn once step 4 is done.
   `create` puts it in Backlog. Start it only as `docs/process/kanban.md` allows. If a limit blocks the start, report that and keep the findings in the ticket.
2. Run `python3 -I factory/rules_health.py`. It prints JSON and exits 1 when it has findings.
   Every finding it prints is a candidate with a file, a line and the evidence it checked. Keep it only if you confirm it by reading the line.
3. Judgment pass, one session, no subagents. Read the documents in scope and look for three things only:
   - Duplicates: the same rule stated in two or more places, worded differently.
   - Stale text: a path, command, script, flag, exit code or ticket the text names that no longer exists or changed.
     The script covers paths, commands and exit-code numbers. You cover meanings (for example what exit 5 means) and ticket states.
   - Contradictions: text against the code, or against a newer ADR or `kanban.md` rule.
4. Keep at most 15 findings, ranked by how likely an agent is to act wrongly on the text. List them as F1..Fn in the ticket.
5. Make one branch `CHB-<n>-rules-health-<YYYY-MM-DD>` and one commit per finding (`F3: ...`), so the owner can reject one and the agent drops that commit.
6. Open one PR through `factory/bin/as-agent gh pr create`. The body lists F1..Fn with file and line, evidence and fix. Never merge it.
7. If the owner rejects a finding, drop its commit and say so in the ticket.

## What counts as a finding

A finding needs all of this. Without one of them, drop it.

- Two quoted sites with file and line (one site for stale text).
- The command or the read that shows the problem, and its output.
- A proposed fix.

Rules for the fix:

- Do not edit the body of an ADR. Add an "Amended" note at its head with the date and the ticket, in the style of the existing notes.
  An ADR section named Context or Alternatives is history: it can be out of date by design and is not a finding.
- `CODEOWNERS` keeps `/.github/workflows/` (owner decision, 2026-11-10).
- When the right side of a contradiction is unclear, make no edit. Put the question in the ticket.
- A line that is history or an example (`until CHB-135`, `Blocks: CHB-88 (To Do)`) is not stale.
- A one-line summary of a rule with a link to its source is not a duplicate.

## Limits

- 15 findings and about 40k tokens per run (about 160 KB of text). Count the documents before you read them.
- Skip `docs/sessions/` (data, not rules) and `docs/postmortems/`.
- One PR across `CLAUDE.md`, ADRs and `factory/` (owner decision, 2026-11-10).

## What the hook and the script do

| Part | File | Reads | Fails |
|------|------|-------|-------|
| Trigger | `factory/hooks/rules_health_due.py` | One Jira search as the agent account | Open: one line on stderr, no context added |
| Deterministic checks | `factory/rules_health.py` | Files only | Exit 1 means findings, not an error |
| Tests | `factory/tests/test_rules_health.py`, `factory/tests/test_rules_health_due.py` | | `make check` |

The script checks four things: dead backticked paths, `factory/bin/jira` subcommands and `make` targets that do not exist,
`exit N` numbers that no `EXIT_*` constant has, and hooks that `.claude/settings.json` does not run.
`factory/tests/test_rules_health.py` runs it on this repository, so `make check` fails when a rule names something that is gone.
