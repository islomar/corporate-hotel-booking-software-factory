<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents** *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [Decision records](#decision-records)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->

# Decision records

One file per decision: `ADR-<4 random hex chars>-<slug>.md`.
IDs are random so two people (or agents) never pick the same number.
Generate one with `openssl rand -hex 2`.

Each record states context, decision, consequences and status (`accepted`, `superseded by ADR-xxxx`).
