<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Gates trust exit codes, not output text](#gates-trust-exit-codes-not-output-text)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Gates trust exit codes, not output text

Found on 2026-11-05 while building the walking skeleton.

A command-output filter on the developer machine rewrote a pytest run with `-W error` as `Pytest: No tests collected`.
The real run passed 3 tests with exit code 0.

Rule: a gate reads the exit code of the raw command.
Text summaries are for humans.
