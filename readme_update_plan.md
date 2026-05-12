# Plan: add Remote build / Send builds section to README.md

A new top-level section that explains the two-dashboard build-offload
feature. Currently README.md has zero mentions of offloader / receiver
/ build server / remote build — a user looking at the README has no
way to know the feature exists.

This is **user-facing intro doc**; the maintainer-facing internals
already live in `docs/ARCHITECTURE.md § Remote build`. Don't repeat
internals here. Stay in the README's existing voice: direct, present
tense, light on jargon, short paragraphs.

## Constraints

- **No human handles or names anywhere.** Not in commit messages,
  PR body, or the README prose. Phrase examples generically ("a
  Home Assistant user", "a contributor"). This is a project-wide
  rule for this codebase.
- **Match the exact in-UI labels** so a user grepping the README
  after seeing the UI finds the right section:
  - Settings entry on the receiver side is labelled **"Build server"**
    (it's `settings.build_server_*` in the frontend's `en.json`).
  - Settings entry on the offloader side is labelled **"Send builds"**
    (the section that lists known + paired receivers).
  - Within Send builds, the discovered-but-not-paired list is
    **"Known dashboards"**.
  - The receiver-side approval inbox is **"Pairing requests"**.
- **Don't quote anyone.** If summarising the project lead's
  description of the feature ("pairing an HA instance to desktop
  and offloading firmware builds should work now"), paraphrase it.
- **Roughly alpha** is the overall project status. The remote-build
  feature works end-to-end (issue #106 closed 2026-05) but has
  known gaps tracked as separate issues — surface that honestly
  rather than overselling.

## Placement

Add the new section **between "Try it" (ends ~line 104) and
"Roadmap" (starts ~line 106)**. The "Try it" section just got the
user running; "Roadmap" looks forward. A "what else can it do"
section between them is the natural reading order.

Suggested section title: **"Send builds to another dashboard"**.
That matches the in-UI label (so a user who clicked through the
UI without reading the README first finds the section by name).

## Section outline + prose suggestions

The draft below is a starting point; the agent picking this up
should edit for voice + tighten further. Don't aim for ARCHITECTURE.md
depth — three to four short paragraphs plus two screenshots is
the target.

### Draft

```markdown
## Send builds to another dashboard

Compiling ESPHome firmware is CPU-heavy, especially for ESP-IDF
targets. If your dashboard runs on a low-power host (e.g. the
Home Assistant add-on on a Raspberry Pi or HA Green), you can
pair it to a beefier dashboard on the same LAN — ESPHome Desktop
on a workstation, say — and offload compiles there. The firmware
bytes still get installed from the original dashboard.

Two roles:

- **Build server** — the dashboard that lends its CPU. Surfaced
  under **Settings → Build server**. Accepts pair requests,
  compiles, returns artifacts.
- **Send builds** — the dashboard that delegates compiles.
  Surfaced under **Settings → Send builds**. Lists known
  dashboards on the LAN and the ones you've paired with.

A single dashboard can be both at once. The HA add-on defaults to
send-only (it doesn't accept inbound build jobs without opt-in —
sensible default for a typically-shared host). ESPHome Desktop
and standalone installs default to both roles on.

### Pairing in four steps

1. Start both dashboards on the same subnet (or with a working
   mDNS reflector between subnets). Each one mDNS-advertises its
   presence as long as the receiver listener is bound.
2. On the dashboard you want to **send** builds from, open
   **Settings → Send builds → Known dashboards**. The list shows
   every dashboard the LAN discovered.

   *<screenshot: Known dashboards list with one or two entries
   visible, before pairing>*

3. Find the dashboard you want to send builds to and click **Pair**.
   Both dashboards now show a pairing fingerprint (a row of
   emoji); compare them out of band — they must match for the
   pairing to be safe to accept.

   *<screenshot: pin-confirm dialog on the offloader side showing
   the emoji fingerprint + Accept button on the receiver side
   showing the same fingerprint>*

4. Click **Accept** on the receiving dashboard's **Pairing
   requests** screen. The pairing is now persisted on both sides
   and survives restarts.

After pairing, clicking Install on a device automatically routes
through the paired receiver when it's online and idle. The install
dialog shows a "Building on {receiver}" sub-line so you can see
which side is doing the work. You can override per-install via
the **Build locally instead** link in the install dialog, or
turn auto-routing off entirely from **Settings → Send builds →
Allow remote builds**.

### Manual entry (no mDNS)

If the dashboards are on different subnets and your LAN has no
mDNS reflector, type the receiver's hostname and port directly
into the field at the bottom of **Known dashboards**. The peer-
link uses plain TCP on port 6055 by default; pairing then runs
identically to the discovered-dashboard flow.

### Known limitations

The remote-build feature works for OTA installs of ESP32 / ESP8266
targets via Wi-Fi or Ethernet. Open follow-ups tracked separately:

- Serial installs (USB-attached devices) don't route through a
  paired receiver yet — they need the full multi-image flash set,
  not just `firmware.bin`. See
  [#570](https://github.com/esphome/device-builder/issues/570).
- A toggle to allow major-version mismatches between paired
  dashboards is planned but not shipped. Pairings whose receiver
  runs a different ESPHome major version than the offloader still
  build today (no enforcement gate yet) — that gate lands together
  with the toggle. See
  [#607](https://github.com/esphome/device-builder/issues/607).
```

### Notes on phrasing

- **"pin"** vs **"fingerprint"** vs **"pairing code"** — the UI
  uses *fingerprint*; stick with that consistently. Don't call
  it a "pin" in the README prose (the underlying field is
  `pin_sha256` but that's an implementation detail).
- **"emoji fingerprint"** is the user-facing term. The frontend
  renders it as an emoji grid (`<esphome-pin-emoji-grid>`).
  Mention emoji rather than hex bytes — hex is tucked behind a
  "Show details" disclosure and isn't the primary verification
  surface.
- **"OOB"** = out-of-band; spell it out the first time
  ("compare them out of band") rather than using the acronym.

## Screenshots needed (from the project lead)

Two screenshots, both from the offloader side first then the
receiver side. Slot them into steps 2 and 3 of the pairing
walkthrough.

1. **Known dashboards list** on the offloader side, with at
   least one entry visible — ideally one already paired
   (CONNECTED pill) and one not paired yet (Pair button).
2. **Pairing fingerprint comparison** — the offloader's
   pin-confirm dialog showing the emoji fingerprint, and the
   receiver's Pairing requests row showing the same fingerprint.
   A side-by-side compose if possible; otherwise two stacked
   images.

Recommended path: save them under `docs/screenshots/` (the dir
already exists per the dashboard-table.png reference in the
frontend repo). Reference from README with relative paths.

## Cross-doc updates

After the README change lands, the **Documentation** section
(currently lines 126-133) should add a one-line pointer to
`docs/ARCHITECTURE.md § Remote build` so users who want to know
how it works internally can find their way down. Suggested
addition:

```markdown
- **[docs/ARCHITECTURE.md § Remote build](docs/ARCHITECTURE.md#remote-build)** —
  internals of the pair flow, peer-link transport, and build
  scheduler.
```

## What NOT to add

- **Don't add a Discord / chat reference** beyond the existing
  one at the top of README.md (line 8). One link is enough.
- **Don't add CLI invocation examples** for pairing. The flow is
  UI-only; the manual hostname-entry field at the bottom of
  Known dashboards is the only non-UI escape hatch and it's a
  UI input too.
- **Don't repeat the security model.** The README mentions
  Noise XX / fingerprint verification briefly via "compare them
  out of band"; the maintainer-facing detail (X25519 keypair,
  TOFU vs cert pinning trade-offs, pairing window state machine)
  belongs only in ARCHITECTURE.md.

## Verification

After the section lands, sanity checks:

1. The section's prose contains no human handles or names.
2. UI labels in the prose match the frontend's `en.json`. Run
   `grep -E '"settings\.build_server_|settings\.send_builds'
   /Users/bdraco/device-builder-frontend_3/src/translations/en.json`
   to confirm the exact label strings. (At plan-writing time:
   `settings.build_server_card_heading` = "Build server",
   `settings.build_server_paired_senders_heading` = "Paired
   senders", various `send_builds_*` keys for the offloader
   side.)
3. The screenshots are saved at `docs/screenshots/` (or
   wherever the existing dashboard screenshots live in the
   frontend repo's `docs/screenshots/`; mirror that location)
   and referenced with relative paths.
4. The Roadmap section's "🚧" entries don't double-document
   anything in the new section. The remote-build feature isn't
   listed in Roadmap today; it doesn't need to be added there
   (the section's existence is its own documentation).
5. Local build still passes: not applicable — README isn't part
   of the bundle. Just sanity-check the markdown renders cleanly
   on GitHub (preview the PR diff).

## PR shape

- **Title:** `[docs] Document Send builds / Build server pairing flow in README`
- **Label:** `docs`
- **Body:** point at this plan if useful; or just describe the
  change in 3-4 sentences. The PR template lives at
  `.github/PULL_REQUEST_TEMPLATE.md` — fill every section, tick
  exactly one Types-of-changes box (the `docs` one).
- **Commits:** one for the prose, one for the screenshots if
  they land via the PR (or, if the screenshots already exist in
  the frontend repo and are referenced by URL, just one for
  the prose).

## Branch off main

```bash
git fetch origin
git checkout -b readme-send-builds-section origin/main
# … edits …
git add README.md docs/screenshots/  # if new
gh pr create --base main --body-file /tmp/pr-body.md \
  --title "[docs] Document Send builds / Build server pairing flow in README"
```

## Out of scope for this plan

- **API documentation** for the `remote_build/*` WS commands —
  already covered in `docs/API.md`.
- **Frontend changes.** The plan is README-only. If the
  walkthrough surfaces a UX gap, file a separate issue against
  `esphome/device-builder-frontend`.
- **Architecture doc cleanup.** Two follow-ups are already in
  flight: PR #610 (code phase-ref cleanup) and a pending
  Copilot-fix follow-up to #611 (rotate_identity conditional
  clarification + OFFLOADER_PEER_LINK_OPENED full payload
  documentation). Neither blocks this README plan.
