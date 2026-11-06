<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Factory](#factory)
  - [Acting as the agent](#acting-as-the-agent)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Factory

Agent workflows, the orchestrator and the notifier live here. Built in phases 3 to 7.

## Acting as the agent

`factory/bin/as-agent <command>` runs a command as the GitHub App `chb-factory-agent`:
`gh` uses a one-hour installation token, git commits are authored by `chb-factory-agent[bot]`, and git pushes over HTTPS with the token.
The private key is read from 1Password at run time.

```sh
factory/bin/as-agent git push -u origin CHB-71-agent-token
factory/bin/as-agent gh pr create --fill
```
