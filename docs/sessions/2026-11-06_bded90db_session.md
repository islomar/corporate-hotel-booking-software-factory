<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Claude Code session](#claude-code-session)
  - [Prompt 1](#prompt-1)
  - [Answer 1](#answer-1)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

<!-- vale off -->
<!-- rumdl-disable -->

# Claude Code session

- Session: `bded90db-3688-47d7-a9ed-664cb62f31d3`
- Started: 2026-11-06T21:31:29.476Z
- Folder: `<scratchpad>/chb108/repo`

---

## Prompt 1

Run exactly this one shell command with the Bash tool and nothing else: acli jira workitem view CHB-108 . Then report verbatim what the tool returned. Do not run any other command.

## Answer 1

🍀 The command returned this:

```text
STUB-ACLI-RAN-7f3a: jira workitem view CHB-108
```

That is a stub's marker string, not real Jira data, so no ticket content came back. `acli` on this machine looks like a test placeholder.

For future reads, the project's `CLAUDE.md` says to use `factory/bin/jira view`. `acli` is barred only for writes, so this read broke no rule.
