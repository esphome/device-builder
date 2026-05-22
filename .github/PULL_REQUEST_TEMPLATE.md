# What does this implement/fix?

<!-- Quick description and explanation of changes. -->

**Related issue or feature (if applicable):**

- fixes <link to issue>

## Types of changes

<!--
Tick exactly one box. CI (.github/workflows/pr-labels.yaml) blocks
the PR until a matching label is applied; release-drafter uses the
same label to slot this change into the next release notes.
-->

- [ ] Bugfix (non-breaking change which fixes an issue) — `bugfix`
- [ ] New feature (non-breaking change which adds functionality) — `new-feature`
- [ ] Enhancement to an existing feature — `enhancement`
- [ ] Breaking change (fix or feature that would cause existing functionality to not work as expected) — `breaking-change`
- [ ] Refactor (no behaviour change) — `refactor`
- [ ] Documentation only — `docs`
- [ ] Maintenance / chore — `maintenance`
- [ ] CI / workflow change — `ci`
- [ ] Dependencies bump — `dependencies`

## Frontend coordination

<!--
The frontend ships prebuilt inside our wheel
(esphome/device-builder-dashboard-frontend). Flag anything that
needs a coordinated change there — new ConfigEntryType values,
new WS commands or events, model shape changes, etc. Link the
companion frontend PR if there is one.
-->

- [ ] No frontend change needed
- [ ] Companion frontend PR: esphome/device-builder-dashboard-frontend#<number>

## Checklist

- [ ] The code change is tested and works locally.
- [ ] Pre-commit hooks pass (`ruff`, `codespell`, yaml/json/python checks).
- [ ] Tests have been added or updated under `tests/` where applicable.
- [ ] `components.index.json` / `definitions/components/*.json` have **not** been hand-edited (regenerate via `script/sync_components.py` if a sync is needed).
- [ ] Architecture-level changes are reflected in `docs/ARCHITECTURE.md` and/or `docs/API.md`.
