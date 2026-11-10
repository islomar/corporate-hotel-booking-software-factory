<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [The harness guard reads Bash text, so it has gaps](#the-harness-guard-reads-bash-text-so-it-has-gaps)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# The harness guard reads Bash text, so it has gaps

Found on 2026-11-10 on CHB-158: an agent on a product-epic ticket edited `.github/workflows/ci.yml` through Bash.
The Write and Edit guard did not see it.
CHB-165 extended `factory/hooks/deny_harness_edits.py` to Bash commands.

The guard reads the command text.
It does not run the command or follow what a program does.
It denies a command that writes a path under `.claude/`, `factory/` or `.github/` on a product-epic ticket branch.

It catches:

- output redirections: `>`, `>>`, `>|`, `&>`, `2>`;
- `tee`, `sed -i`, `perl -i`;
- `cp`, `install`, `ln`, `rsync` into a harness path, and `mv` from or into one;
- `rm`, `rmdir`, `touch`, `mkdir`, `truncate`, `dd of=`;
- `cd` before any of these, `bash -c '...'` and shell here-documents;
- python, node, ruby, perl or php code that calls a write function and names a harness path.

It does not catch:

- a path built from a variable or a command substitution: `$DIR/x`, `$(pwd)/x`;
- a program that writes on its own: `python3 script.py`, `make`, `npm run`, `curl -o`, `wget -O`;
- `git checkout`, `git restore`, `git apply`, `git mv`, `git rm`, `git stash pop`;
- `eval`, `xargs`, `find -exec`, `awk` and `ed` scripts;
- interpreter code that builds a path from pieces (`Path("fact" + "ory")`);
- a `cd` inside a subshell or a function, which the guard treats as lasting.

It can deny a command that only looks like a write: a here-document read by a shell that contains such text, or interpreter code that mentions a harness path and writes elsewhere.
Rewrite the command, or make the change in a factory ticket.

The backstop is code-owner review of `.claude/`, `factory/` and `.github/`.
The guard stops accidents, not a deliberate bypass.
