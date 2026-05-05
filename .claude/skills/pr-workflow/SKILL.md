---
name: pr-workflow
description: Create pull requests for esphome/device-builder. Use when creating PRs, submitting changes, or preparing contributions.
allowed-tools: Read, Bash, Glob, Grep
---

# device-builder PR Workflow

When creating a pull request for `esphome/device-builder`, follow
these steps. The repo's conventions are documented in
[CLAUDE.md](../../../CLAUDE.md); this skill summarises the parts
that matter at PR-creation time.

## 1. Create branch from origin/main

There is no fork in this workflow — `origin` already points at
`esphome/device-builder`. Always re-fetch first so the branch is
based on the latest `main`:

```bash
git fetch origin
git checkout -b <branch-name> origin/main
```

## 2. Read the PR template

Before creating a PR, read `.github/PULL_REQUEST_TEMPLATE.md` to
understand the required sections. Fill in **every** section — do
not skip or abbreviate.

## 3. Tick exactly one "Types of changes" box

`.github/workflows/pr-labels.yaml` parses the PR description for a
`- [x] ... \`<label>\`` line and applies the canonical label
automatically. The job fails if zero boxes are ticked **or** if
more than one is ticked — always tick exactly one. Pick whichever
fits best from:

`breaking-change`, `new-feature`, `enhancement`, `bugfix`,
`refactor`, `docs`, `maintenance`, `ci`, `dependencies`.

The label is what release-drafter uses to slot the PR into the
right release-notes section, so the choice is editorial — pick the
one a future release-notes reader would expect.

## 4. Frontend coordination

The frontend (`esphome/device-builder-frontend`) ships prebuilt
inside our wheel. If the PR touches anything the frontend consumes
— new `ConfigEntryType` values, new WS commands or events, model
shape changes — flag it under **Frontend coordination** and link
the companion PR there.

## 5. Commit message conventions

- **Imperative-mood subject line** — "Add X", not "Added X".
- **No `Co-Authored-By: Claude` trailer.** Project preference.
- One logical change per commit; let pre-commit run (ruff,
  codespell, yaml/json/python checks). If a hook auto-fixes
  something, re-stage and re-commit.

## 6. Push and create the PR

**Always read `.github/PULL_REQUEST_TEMPLATE.md` from the repo at
PR-creation time and use it verbatim as the body** — do not
reproduce, paraphrase, or trim the template anywhere else, or it
will silently drift out of sync as the template evolves.

When filling in the template:

- Replace the `<!-- ... -->` prompt comments with the actual prose
  for that section. Do not delete anything else.
- **Leave all the checkboxes in place.** Do not remove rows you
  aren't ticking — release-drafter / the auto-labeller and the
  human reviewer both rely on the full list being present.
- Tick exactly one "Types of changes" box (see step 3). For the
  Frontend coordination and Checklist sections, only tick boxes
  you have actually verified; leave the rest as `- [ ]`.
- **Do not escape characters from the template.** Backticks,
  asterisks, angle brackets, etc. must be passed through verbatim
  — escaping a backtick to `` \` `` breaks the auto-labeller's
  regex (which looks for ``` `<label>` ```) and corrupts inline
  code in the rendered PR. The template is already valid Markdown;
  do not rewrite it for shell quoting. Use `--body-file`, never
  `--body "..."` with shell-escaping.

```bash
git push -u origin <branch-name>
# Read .github/PULL_REQUEST_TEMPLATE.md, fill it in as above,
# write the result to a temp file, then:
gh pr create --repo esphome/device-builder --base main \
  --title "Imperative subject under 70 chars" \
  --body-file /tmp/pr-body.md
```

The keep-the-checklist-honest rule applies — only tick a checklist
box you've actually verified. An untouched `components.json` is
verified by running `git diff --stat origin/main..HEAD -- \
esphome_device_builder/definitions/components.json`; doc updates
are verified by inspecting the diff.

## 7. After the PR is open

CI runs lint, the test matrix (incl. Windows), and the label
applier. If `pr-labels` fails, the description checkbox is missing
or unrecognised — edit the PR body, don't push an empty commit.
