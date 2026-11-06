<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Ideas from the 540 talk](#ideas-from-the-540-talk)
  - [Their delivery cycle](#their-delivery-cycle)
  - [Adopted](#adopted)
  - [Adapted](#adapted)
  - [Kept different on purpose](#kept-different-on-purpose)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Ideas from the 540 talk

Source: the talk "Del humano en el bucle al desarrollo desatendido" and 46 slide screenshots, reviewed on 2026-11-06.
Each idea has a verdict and the Jira ticket that carries it.

## Their delivery cycle

Discovery, Spec, Design, Implementation, Verification, Delivery, Operation.

- Spec was the bottleneck, not implementation: tickets said what to do but not what done means, and were too big for one workflow run.
- Human diff review does not scale with generated code. They measure instead: a scored review plus evidence per acceptance criterion.
- Design drift (each task fine, the sum degrading) is still open for them.
- Once implementation runs unattended, the human becomes the bottleneck. The orchestrator calls the human only to sign a PR or clarify a ticket.

## Adopted

| Idea | Ticket |
|---|---|
| Typed outputs (`analysis`, `implementation`, `review`, `triage`). Code routes on fields, never on prose. | CHB-62 |
| Preflight: `main` is green before the agent touches anything. Review loop capped at 3 rounds. | CHB-63 |
| Review rubric: five weighted dimensions from 10, critical caps a dimension at 7, fail below 8.5 or on any critical. | CHB-40 |
| Triage of findings: apply, discard or doubt. Doubts go to the PR caveats. | CHB-60 |
| Ticket template: acceptance criteria table with ids, named fixtures and exact expected output. | CHB-58 |
| Spec lives in the epic; grill-me decisions are written down. | CHB-59 |
| PR body: AC checklist with verification method, gates, scores, one screenshot per AC. | CHB-61 |
| Hooks: branch name, related tests only, loop detector on repeated edits of one file. | CHB-54 |
| Before-done gate and touched-file lint ratchet. | CHB-55 |
| Tick order: reconcile first, then PR fixes, PR reviews, new tickets. | CHB-47 |
| Worker caps, watchdog, slots, daily budget. | CHB-65 |
| CI and review-thread watcher that replies to each thread. | CHB-66 |
| Worktree per ticket, `watch` command, start by Jira label. | CHB-67 |

## Adapted

| Idea | Change | Ticket |
|---|---|---|
| Needs-human column | A Jira status, top of notifications. | CHB-32 |
| Plan review by 3 reviewers | One reviewer, only on risky tickets, until evals justify more. | CHB-63 |
| Model per phase | Config field now, tuned with evals. | CHB-64 |
| Quota points | Daily token or USD budget from Claude Code usage. | CHB-65 |
| Menu-bar app and Slack | Read-only status view, ntfy priorities, daily Telegram summary. | CHB-68 |
| Sentry triage | Cloud Error Reporting, after the dial is raised. | CHB-69 |
| Design drift | Duplication, CRAP and architecture fitness gates. | CHB-57 |
| CI cost grows faster than PR count | CI cost guard from the start. | CHB-56 |

## Kept different on purpose

- Their Close step moves a merged ticket to Done. Ours moves it to Validating. See ADR-ad3e.
- Skipped: blocking new code comments, the release and certificate workflow, the mascot.
