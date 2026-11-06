<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Factory](#factory)
  - [Acting as the agent](#acting-as-the-agent)
  - [Automated PR review (shadow mode)](#automated-pr-review-shadow-mode)

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

## Automated PR review (shadow mode)

`.github/workflows/review.yml` scores every same-repository PR into `main` with Claude Code and posts a check run named `review` as the App `chb-factory-reviewer` on the PR head commit.
The `main` ruleset does not require the check yet. Design: ADR-ea8c (PR #16). Tickets: CHB-87, CHB-93.

How it starts:

1. `.github/workflows/review-trigger.yml` runs on `pull_request`. It has no permissions and no secrets, and only echoes a line.
2. When it completes, `review.yml` runs on `workflow_run`. GitHub takes it from `main`, so the workflow, gate, schema and prompt always come from `main`.
3. Its `resolve` job takes the head SHA from the event and finds the PR through the API.
   `review/resolve_pr.py` checks it: open, not draft, into `main`, from this repository, not Dependabot, head equal to that SHA.
4. Its `score-pr` job runs in the `reviewer` environment, reads the PR head as data and never runs it.

A change to `review.yml` takes effect only after merge: the PR that changes it is reviewed by the copy on `main`.

| File | Role |
|---|---|
| `review/prompt.md` | Review instructions. PR text and code are untrusted data. |
| `review/schema.json` | Output shape: five dimensions, each with a score and findings, plus a summary. |
| `review/gate.py` | Validates the output, recomputes the scores, decides, and builds the check run. |
| `review/resolve_pr.py` | Picks the PR to review from API data: open, not draft, into `main`, same repository, not Dependabot, head equal to the run's SHA. |

The gate ignores every score the model claims. Each dimension starts at 10 and loses 3 per critical finding, 2 per improvement and 0.5 per cleanup.
A critical finding caps its dimension at 7. Merge Safety is the weighted mean: code quality 30, security 25, business alignment 15, tests 15, architecture 15.
The check passes when Merge Safety is at least 8.5 and no finding is critical.
Malformed JSON, a schema violation, an extra field, an error result or a non-zero exit of `claude` fail the check.

The summary also shows the risk class from `git diff --no-renames --name-only base...head` and the round number from earlier `review` runs of the App.

The workflow needs three secrets in the `reviewer` environment, which admits `main` only: `ANTHROPIC_API_KEY_REVIEW` (CHB-89), `REVIEWER_APP_CLIENT_ID` and `REVIEWER_APP_PRIVATE_KEY` (CHB-85).
Do not store them as repository secrets: a branch can add a `pull_request` workflow that reads repository secrets.
Without them the job prints a notice and posts nothing.

Run the gate on a saved result:

```sh
python3 -I factory/review/gate.py --result result.json --claude-exit-code 0 --schema factory/review/schema.json \
  --changed-files names.z --prior-runs runs.json --app-slug chb-factory-reviewer --head-sha HEAD_SHA --out check-run.json
```
