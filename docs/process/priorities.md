<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Ticket priorities](#ticket-priorities)
  - [High](#high)
  - [Medium](#medium)
  - [Low](#low)
  - [Rules](#rules)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Ticket priorities

Every CHB ticket has one of three priorities: High, Medium or Low.
Jira also offers Highest and Lowest. CHB does not use them, and `factory/bin/jira` refuses them.
The Jira Free plan has no priority schemes, so the global list stays and this rule limits it (CHB-128).

## High

Leaving the ticket open risks harm now.

- Security: a secret, token or private data can leak; private content can reach a public repository; an authentication or permission bypass; a supply-chain path that merges unreviewed code.
- Data loss or a destructive change: a command, script or Terraform plan can delete or overwrite data, infrastructure or repository state.
- A broken guardrail: a factory rule (identity, branch, WIP limit, review) can be bypassed or fails without a signal.
- Production or `main` broken: the deployed app is down or wrong, CI on `main` fails, or the agent cannot work.

## Medium

The default.
The ticket improves the product or the factory, and nothing breaks or leaks while it waits.

- A missing guardrail where the mistake has not happened yet.
- Tooling or process work that removes manual steps.
- Kata features.

## Low

The ticket can wait indefinitely at no cost.

- Wording or cosmetic changes, nice-to-have tooling, ideas to explore, cleanups nobody waits for.

## Rules

- Priority informs the rank. The rank in To Do decides the order of work.
  A High ticket ranked below a Medium or Low ticket gets a comment that says why.
- The agent sets the priority when it creates a ticket (`factory/bin/jira create --priority`), with the reason in one line of the description.
- Between two levels, take the higher one and say so in the ticket.
- The agent changes a priority with `factory/bin/jira priority CHB-<n> High|Medium|Low` and writes the reason as a comment.
