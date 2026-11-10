<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Release age applies at lock time, not at install time](#release-age-applies-at-lock-time-not-at-install-time)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Release age applies at lock time, not at install time

Found on 2026-11-06 while setting up the dependency cooldown (ADR-ebe6).

- `npm ci` with `min-release-age=7` installed vite 8.3.6, published one day earlier, from the lockfile.
- `uv sync --locked` and `uv sync --frozen` accepted a `uv.lock` with uploads inside the `exclude-newer` window.
- npm reads `min-release-age` in days, as an integer. `min-release-age=7d` fails with `Invalid time value`.
- npm older than 11.10 ignores the setting and prints `Unknown project config "min-release-age"`.
- `uv.lock` stores a relative `exclude-newer` as `exclude-newer-span = "P7D"`.
  `uv lock --check` passed again after the cutoff moved.
  Checked over seconds, not days: the cutoff only moves forward and the locked uploads stay behind it.
- `uv lock --upgrade-package boto3==1.43.127` with `exclude-newer = "7 days"` failed on 2026-11-10 (uv 0.13.7):
  `boto3` was filtered by `exclude-newer`, the requested version was one day old.
  Dependabot locks uv with that command, so it cannot open a uv security PR for a fix younger than the window (CHB-145).

Rule: CI checks every lockfile change with `scripts/check_release_age.py`.
The install commands do not.
