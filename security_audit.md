# Security audit — device-builder backend & frontend

Date: 2026-05-02. Audited by an automated `Explore` subagent on each
codebase, then every finding manually verified against the actual code.
The audit was useful as a checklist but had a high false-positive rate
(about 65%) — the verifications below are what gates each finding into
actionable / not-actionable buckets.

## Audited surface

- **Backend**: `/Users/bdraco/device-builder_2/esphome_device_builder/`
  — controllers, helpers, api directories. Skipped tests, docs, and
  the generated `definitions/components.json`.
- **Frontend**: `/Users/bdraco/device-builder-frontend_2/src/`. Skipped
  the test directory and generated files.

## Threat model

- **In scope**: LAN-attached attackers (rogue mDNS broadcasters,
  authenticated users abusing the WS surface), supply-chain risks via
  the catalog data the frontend renders, dashboard-process memory
  exhaustion. Web-Serial path is out of scope (browser permission grant
  is the gate, not us).
- **Out of scope**: physical access to the dashboard machine, OS-level
  privilege escalation, ESPHome's own external-component sandbox (it
  doesn't have one — pairing implies shell-level trust, see
  esphome/device-builder#106 for the matching design discussion).

## Findings — actionable

| Sev | ID | Where | What | Fix |
|---|---|---|---|---|
| HIGH | B-4 | `firmware.py` streaming loop, post-fix runner | `job.output` was unbounded mid-run; only the post-completion `_trim_job_output` ever fired. A pathological build streaming gigabytes of stderr (chatty `external_components` retry loop, esptool stuck on a repeating error) OOMed the dashboard before the subprocess exited. | **Fixed in PR #117.** In-flight cap at `2 × _MAX_OUTPUT_LINES_RETAINED`, trim back to retained on cross with hysteresis so the O(keep) slice copy is amortised across `cap - keep` appends. Closes the OOM vector and exposed a separate `follow_job` race that the same PR also fixes. |
| MED | F-1 | `frontend/src/util/markdown.ts:80–86` | `[text](javascript:...)` URLs in catalog markdown render as live anchors. Catalog data is repo-controlled (board manifests, schema-derived component descriptions) so external attackers can't inject — bounded supply-chain risk only. Defense in depth wants scheme validation. | Validate `seg.href` against `http:` / `https:` / `mailto:` before rendering; fall back to plain text otherwise. |
| MED | B-2 | `controllers/devices.py:886` `import_device` | `package_import_url` from a discovered mDNS TXT broadcast lands in `import_config()`, which `git clone`s the URL and runs the cloned Python at compile time. A LAN-side attacker can advertise a malicious URL via mDNS; the Take-Control dialog doesn't show the URL before the user clicks. Inherited from upstream's `dashboard_import` design — same surface as the legacy ESPHome dashboard. | Surface the `package_import_url` in the Take-Control dialog so the user can see what they're trusting before adopting. (Doesn't fix the underlying RCE-on-trust property; that's a documentation concern.) |
| MED | F-2 | `frontend/public/index.html` | No Content-Security-Policy header, no Subresource-Integrity pins on external scripts. Local-network tool with no third-party scripts and Lit auto-escaping covers most XSS vectors, but CSP would harden against any future-discovered XSS. | Add `<meta http-equiv="Content-Security-Policy" content="default-src 'self'">` to `index.html` (and configure SRI on any third-party assets if introduced). |
| LOW | B-5 | `api/ws.py:268–274` `_origin_matches_host` | DNS-rebinding bypass: attacker's `evil.com` rebinds to victim's LAN IP, browser sends matching `Host` and `Origin` headers. **Mitigated** by WS auth: the rebound connection still has to send a valid `auth/login` under per-IP rate limiting, so the bypass doesn't yield command access. Defense-in-depth tightening adds a `Host` allowlist. | Optional: validate `Host` against a configured allowlist when password is set, in addition to the Origin/Host equality check. |
| LOW | B-12 | `controllers/devices.py` `import_result` | mDNS-discovered devices are kept in an in-memory dict until zeroconf fires Removed. A sustained mDNS-spam attacker could fill it, bounded by zeroconf's pacing. Practically not exploitable; defense-in-depth wants a hard cap. | Add a soft cap (e.g. 1000 entries) on `import_result`; drop oldest on overflow. |

## Findings — false positives or non-issues

These were flagged by the audit subagent but didn't survive verification.
Listed so a future re-audit doesn't re-litigate them.

| ID | Where | Why it's not a finding |
|---|---|---|
| B-1 | `api/legacy.py:85–130` `/devices`, `/json-config` | Audit claimed these bypass `auth_middleware`. They don't — `device_builder.py:316–318` mounts the middleware on the aiohttp `Application` whose `create_legacy_routes()` is added at line 332. Aiohttp middlewares apply to every route on the app. `_PUBLIC_PATHS` is `{/, /ws, /favicon.ico, /manifest.json}` — neither legacy route is in it. Legacy ESPHome dashboard had the same gate via per-handler `@authenticated` decorator (`web_server.py:1132`). |
| B-3 | `controllers/firmware.py:1305` `firmware/rename` `new_name` | `new_name` is passed to `esphome rename` via subprocess argv (no shell). Upstream's `command_rename` validates against `ALLOWED_NAME_CHARS` (`__main__.py:1564–1573`) and rejects characters like `/`, spaces, and YAML metacharacters. No injection vector. |
| B-6 | `controllers/devices.py:854` import name validation | `rel_path()` does `joined.resolve().relative_to(self.absolute_config_dir)` — any path traversal raises `ValueError`. NUL bytes / unicode tricks would create files with weird names but stay inside config_dir. Not a security boundary. |
| B-7 | `controllers/devices.py:756–775` `get_api_key` | `load_device_yaml` already wraps in `try/except: return None` (`device_yaml.py:553–558`). No exception bubbles to the caller; on error the function returns `{"key": ""}`. |
| B-8 | `controllers/config.py:158–166` `_load_metadata` | `.device-builder.json` is internally-managed by the dashboard process. Not user-supplied input. An attacker who can write large content there already has filesystem write to the config_dir, far worse than what they'd gain from a JSON DoS. |
| B-9 | `import_config` git clone | Adoption-time fetch path (`requests.get`) has a 30s timeout (`dashboard_import:113`). Compile-time `dashboard_import` git clone is upstream behavior at compile time, not a backend boundary. |
| B-10 | Firmware job submission rate limit | Single-job queue + auth required. Worst case: authenticated user starves their own queue. Not a security issue. |
| B-11 | Subprocess orphan window between `create_subprocess_exec` and `_current_process` assignment | No exception-throwing code runs between `await create_subprocess_exec(...)` returning and the assignment. If the await raises, no subprocess exists to orphan. |
| B-13 | Compile output secret leakage | `esphome compile`/`run` don't dump resolved YAML to stdout — only `command_config` does, and PR #95 already redacts the conceal-wrapped form via `_redact_concealed_secrets`. The `_wrap_to_code` YAML dump at `__main__.py:677` lands inside generated C++ comments, not the build log. |
| B-14 | Filename character validation | Duplicate of B-6. |
| F-3 | `device-table.ts:475` `data-configuration` attribute interpolation | Lit auto-escapes attribute interpolations. The agent's "implicit contract" concern is overcautious. |
| F-4 | Theme localStorage no integrity check | UI-only. Bad value = broken theme rendering, not a security issue. |

## Verification methodology

For each flagged finding the actual code path was traced from the WS /
HTTP entry point to the alleged sink:

1. **Authentication wiring** (`B-1`): traced
   `device_builder.create_app` → middleware list construction →
   route registration order. Confirmed aiohttp's middleware-applies-to-
   all-routes semantics covers `create_legacy_routes()`.
2. **Subprocess argv vs shell** (`B-3`): inspected `_build_command` to
   confirm argv-list construction (no `shell=True` invocation), then
   chased the eventual `command_rename` validation upstream.
3. **`rel_path` traversal protection** (`B-6`, `B-14`): exercised
   `joined.resolve().relative_to(absolute_config_dir)` mentally with
   `..`, NUL, and unicode-normalisation inputs; verified `relative_to`
   raises `ValueError` on escape.
4. **`load_device_yaml` error path** (`B-7`): read the helper's
   `try/except: return None` and confirmed `get_api_encryption_key`
   handles `None` cleanly.
5. **`job.output` mid-run growth** (`B-4`): traced `_trim_job_output`
   call sites and confirmed it ran only in the post-completion
   `finally` block. Manually walked through a 1M-line adversarial
   stream to estimate per-line cost. Wrote a focused test to lock
   the contract.
6. **Origin DNS-rebinding** (`B-5`): traced `_origin_matches_host`,
   then chased the WS auth handshake to confirm `_PRE_AUTH_COMMANDS`
   is `{auth, auth/login}` and `auth/login` enforces per-IP rate
   limiting.
7. **mDNS RCE on adopt** (`B-2`): walked the `package_import_url` data
   path from the upstream `DiscoveredImport` (zeroconf TXT-derived) to
   our `import_result` cache to the WS handler to `import_config`. Read
   upstream `dashboard_import` to understand the git-clone-then-Python-
   import behavior at compile time.

## Open follow-ups

- **B-2 mDNS package-URL trust**: documentation + UI surface change in
  the Take-Control dialog. Not coded yet; bigger UX decision worth
  coordinating with the remote-build feature
  (esphome/device-builder#106) which has the same trust statement.
- **F-1 markdown link scheme validation**: small one-line util change.
- **F-2 CSP**: meta-tag addition in `index.html`.
- **B-5 Host allowlist**: configuration knob + validation. Mitigated by
  auth + rate limit; defense-in-depth, not urgent.
- **B-12 `import_result` cap**: small constant + LRU eviction. Defense
  in depth; not currently exploitable in practice.
- **`device_builder.py:269–273` listener leak** (discovered while
  applying the `EventBus.listening` context manager refactor):
  `subscribe_events` attaches one listener per `EventType` for the WS
  connection lifetime but has no `finally` cleanup. When the WS closes,
  listeners stay attached and reference the closed-client closure.
  Track separately; not in scope for this audit since it's a leak, not
  a security boundary.

## Notes on the audit subagent's accuracy

Two of the agent's flagged "CRITICAL" findings were false positives
that needed careful tracing through middleware wiring (B-1) and
upstream esphome CLI validation (B-3) to disprove. Several "real bug"
findings (B-7, B-8, B-11, B-13) didn't survive a closer read of the
actual code path — defensive programming the agent didn't notice
already covered them.

Useful as a checklist of "where to look", not as a punch list of "what
to fix". Future audits should treat the agent's output as a list of
hypotheses to verify, not actions to execute.
