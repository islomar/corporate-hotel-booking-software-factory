---
name: ticket-gate
description: Check that every acceptance criterion of a Jira ticket can be checked. Use before `factory/bin/jira ready` when you wrote the criteria yourself, or when asked to gate a ticket (`/ticket-gate CHB-<n>`). It is read-only and never edits the ticket.
---

<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [ticket-gate](#ticket-gate)
  - [Steps](#steps)
  - [The three tests](#the-three-tests)
  - [Report format](#report-format)
  - [After a REJECT](#after-a-reject)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# ticket-gate

Input: one ticket key, for example `CHB-164`. If none is given, ask for it.

## Steps

1. Read the ticket: `factory/bin/jira view CHB-<n>`. Never use `acli`.
2. Take the lines under the section headed "Acceptance criteria".
   Skip "Acceptance criteria (Validating)": it has its own rules in `docs/process/kanban.md`, Validating.
   If the ticket has no such section, print `REJECT: no Acceptance criteria section` and stop.
3. Split the section into criteria: one per list item (or per numbered line such as `AC1.`).
   A sub-list belongs to its parent item; judge the item and each sub-item.
4. Judge each criterion with the three tests below. A criterion is checkable only when it passes all three.
5. Print the report in the format below. Do not edit, comment on or transition the ticket.
   The agent that owns the ticket decides what to change, and writes it with `factory/bin/jira section`.

## The three tests

1. Observable outcome: a state, an output, an exit code, a value or a file that is different when the work is done.
   Fails on "improve", "support", "handle", "applied", "recorded" with no recorded thing named.
2. A place to look: where the evidence lives and how to read it (a file path, a test name, a command, a URL, a Jira section).
   Fails on "a test" or "the report" when no location is named.
3. No vague words: words that two people judge differently.
   Fails on "works well", "properly", "correctly", "fast", "clean", "readable", "simple", "easy", "good", "sensible", "reasonable", "as needed", "appropriate".
   A number, a limit or a named command replaces the word.

`make check passes.` is checkable: the command and its exit code are the evidence.
A criterion that is checkable only after a future event (for example "on the next Dependabot PR") passes
when it names the event and where to read the result.

## Report format

```text
ticket-gate CHB-<n>: <PASS | REJECT>
1. [CHECKABLE] <criterion, shortened>
2. [NOT CHECKABLE: <no observable outcome | no place to look | vague word "<word>">] <criterion, shortened>
   Rewrite: <one checkable sentence that names the outcome and where to read it>
```

- PASS: every criterion is checkable. REJECT: at least one is not, or the section is missing.
- A rewrite keeps the owner's intent. It adds only what the ticket, its epic or an ADR already implies.
  When the rewrite needs a fact you do not have (a threshold, a file name), put the question in `Rewrite:` as `Needs: <question>` and do not invent the fact.
- One line per criterion. Do not praise, and do not restate the ticket.

## After a REJECT

The agent that wrote the criteria rewrites them (`factory/bin/jira section CHB-<n> --heading "Acceptance criteria" --body-file FILE`)
and runs `/ticket-gate` again. A criterion that needs an owner decision goes to the owner; see `docs/process/kanban.md`, Ready.
