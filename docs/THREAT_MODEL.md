# Threat model

This document names the dashboard's trust boundaries so contributors
can tell a real security bug from a defense-in-depth improvement,
and so reviewers don't gate routine refactors behind a security
analysis they don't need.

See [ARCHITECTURE.md](ARCHITECTURE.md#authentication) for the
mechanics of the auth gate, ingress site, and reverse-proxy
`Origin` allowlist; this document is about *what those gates
defend*, not *how they work*.

## The trust boundary

There is exactly one security boundary in the dashboard:
**unauthenticated network traffic** vs. **authenticated dashboard
clients**. The gates that enforce it are:

- The WS in-band `auth` handshake on the public site
  (`--host:--port`).
- The HA-addon ingress site (`--ingress-host:--ingress-port`),
  which trusts the supervisor's upstream auth — the boundary is
  enforced by the supervisor and by physically binding the
  ingress site to the supervisor's docker network. See
  ARCHITECTURE.md for the Music-Assistant-pattern rationale.
- The reverse-proxy `--trusted-domains` `Origin` / `Host`
  allowlist (defense against DNS rebinding and browser-driven
  cross-origin attacks).
- The peer-link OOB pin verification on first pair (defends
  against LAN MITM during the one window where pinning hasn't
  established trust yet).

Bugs that let an *unauthenticated* network attacker cross any of
these gates are security bugs.

## Authenticated callers are host-equivalent

Once past the auth gate, the dashboard intentionally exposes
**full code execution on the host** to the caller. This is not a
bug — it's what the product does. Concretely, any authenticated
client can:

- **Run arbitrary Python at compile time** via `external_components:`
  in any compiled YAML. ESPHome imports the component package as
  ordinary Python, so its top-level code runs with whatever
  privileges the dashboard process has. The remote-build /
  offloader path doesn't change this — the compile happens
  *somewhere*, and that somewhere runs the user's Python.
- **Run arbitrary shell** via `esphome` subprocess invocations
  the dashboard wires up for compile / clean / dashboard_import /
  vscode validation. Any field the caller can put in a YAML that
  eventually feeds those subprocesses is, transitively, attacker-
  controlled input to a tool that loads Python.
- **Read and write arbitrary files under the data directory**
  (`<config_dir>/.esphome/` in default mode, `/data/` in the HA
  addon, `$ESPHOME_DATA_DIR` if set) via the YAML editor, clone,
  archive, and firmware-job artefacts.
- **Read and write arbitrary files under the config directory**
  via the YAML editor and dashboard_import — that's the entire
  point of those features.

The dashboard does **not** sandbox these capabilities and does
not aspire to. A compromised authenticated session is equivalent
to shell access on the host the dashboard runs on. Operators who
need a stronger boundary between users and the host run separate
dashboard instances in separate containers.

## Implication: what is not a security bug

Anything reachable only through the authenticated surface is not
a security bug, because it doesn't cross any boundary the
dashboard defends:

- Editor `!include` paths that read files outside the config
  directory (PR #868). The same caller can `external_components:`
  arbitrary Python that does `Path("/etc/passwd").read_text()`
  and exfiltrates it through a compile-time `print`.
- YAML emission writing surprising paths under `<config_dir>` /
  `<data_dir>`. Same caller can write anywhere the dashboard
  process has filesystem permissions to.
- `dashboard_import` accepting a hostile remote YAML. The caller
  is the one providing the URL; they could just paste the same
  YAML into the editor.
- Validator subprocess crashes triggered by adversarial input
  from the same authenticated client. Subprocess hardening is a
  reliability concern, not a security one — the client could
  just run the same `esphome config` directly via a YAML they
  control.

These can still be **defense-in-depth** improvements worth
landing, and several already have:

- The editor's `!include` containment check (PR #868) — the
  validator's `read_file` callback has no legitimate reason to
  reach outside the config tree, so refusing surprising paths
  reduces the operator's "wait, why did my dashboard just `cat`
  that" surprise area without claiming a boundary.
- The peer-link pairing window and OOB pin verification — those
  *are* on the security boundary (an unauthenticated LAN peer
  initiates the handshake), but the redundant gates around an
  already-APPROVED pin are belt-and-braces.

Land these as `maintenance` / `enhancement` with a clear
"defense-in-depth" framing in the PR body. Do not file CVEs, do
not coordinate a disclosure, do not credit a researcher with a
finding — the issue wasn't a boundary breach.

## What we still defend (the actual security surface)

Bugs in any of the following *are* security bugs:

- **Auth bypass.** Anything that lets a request reach a
  privileged WS command or REST endpoint without a valid session
  on the public site, or that lets external traffic reach the
  ingress site without going through the supervisor.
- **Origin / Host gate evasion.** Anything that lets a browser-
  driven cross-origin request slip past `--trusted-domains` when
  the operator opted in.
- **Peer-link pin pinning failures.** A pair flow that lets a
  fresh pubkey ride an existing approved row's trust without OOB
  reconfirmation (the `pin_sha256` TOCTOU between preview and
  pair-request handshakes is the canonical example —
  ARCHITECTURE.md covers the defense in detail).
- **Reading or writing files via an `unauthenticated` path** —
  this is the inverse of the previous section. If the path is
  reachable without auth, the rules above don't apply.
- **mDNS / network spoofing escalating to authenticated state.**
  The discovery surface is intentionally low-trust (a hostile
  LAN peer can announce whatever it wants); it must not be a
  path to authenticated capability.

## Out of scope

These are explicitly *not* threats the dashboard defends against:

- **Local-host attackers with shell access.** Same trust level
  as authenticated users by construction.
- **Supply-chain attacks against `esphome` / pinned dependencies.**
  We pin and review, but the trust transitive on `pip install` is
  out of scope.
- **Operator-supplied YAML containing hostile content.** Bringing
  in an older legacy YAML or a contributed example that does
  surprising things is the user's responsibility; the editor will
  let them open and edit it (see CLAUDE.md's "user-supplied
  content is not a generator" carve-out).
- **Denial of service against the dashboard process from an
  authenticated client.** They can already crash their own
  dashboard; reliability is a quality bar, not a security
  boundary.
- **An operator who deliberately opened the front door.** On the HA
  add-on, enabling "Disable external authentication"
  (`leave_front_door_open`) *and* mapping port 6052 binds the public
  port with no authentication at all; the operator removed the
  boundary themselves, so the resulting wide-open LAN dashboard is
  not a breach of anything we defend. It is LAN-equivalent to handing
  out shell access on the host, by explicit request; the dashboard
  logs a loud banner saying exactly that. The public site stays
  untrusted, so the WS `Origin` gate still rejects a plain
  cross-origin browser drive-by; the residual reach is DNS rebinding
  (where `Origin` and `Host` both become the attacker's rebound name),
  which an operator who cares closes with `--trusted-domains` (the
  `Host` allowlist). Both opt-ins are required (legacy parity), so it
  can't happen by accident.
