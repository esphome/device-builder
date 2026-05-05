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
automatically. Failing to tick a box (or ticking more than one
where the intent is ambiguous) blocks the PR. Pick whichever fits
best from:

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

```bash
git push -u origin <branch-name>
gh pr create --repo esphome/device-builder --base main \
  --title "Imperative subject under 70 chars" \
  --body "$(cat <<'EOF'
# What does this implement/fix?

<one paragraph: what changed and why>

**Related issue or feature (if applicable):**

- fixes #<issue-number>

## Types of changes

- [ ] Bugfix (non-breaking change which fixes an issue) — `bugfix`
- [ ] New feature (non-breaking change which adds functionality) — `new-feature`
- [x] Enhancement to an existing feature — `enhancement`
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected) — `breaking-change`
- [ ] Refactor (no behaviour change) — `refactor`
- [ ] Documentation only — `docs`
- [ ] Maintenance / chore — `maintenance`
- [ ] CI / workflow change — `ci`
- [ ] Dependencies bump — `dependencies`

## Frontend coordination

- [x] No frontend change needed
- [ ] Companion frontend PR: esphome/device-builder-frontend#<number>

## Checklist

- [x] The code change is tested and works locally.
- [x] Pre-commit hooks pass (`ruff`, `codespell`, yaml/json/python checks).
- [x] Tests have been added or updated under `tests/` where applicable.
- [x] `components.json` has **not** been hand-edited (regenerate via `script/sync_components.py` if a sync is needed).
- [x] Architecture-level changes are reflected in `docs/ARCHITECTURE.md` and/or `docs/API.md`.
EOF
)"
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
