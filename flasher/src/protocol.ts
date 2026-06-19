// Message contract between the Device Builder dashboard (the opener, on any
// http/https origin) and this flasher page (a fixed secure-context origin).
// The opener origin is unknown, so authentication is the one-time nonce plus an
// "is this my opener" source check, never an origin allowlist. This same
// contract is what PR 2 reimplements inside web.esphome.io.
//
// URL hash params the flasher reads: 'nonce' (required) and 'origin' (optional).
// The nonce is a ONE-WAY opener->flasher token: inbound firmware must carry it,
// but NO outbound frame (ready/state/progress) ever echoes it, so the pre-handoff
// 'ready' broadcast to '*' leaks no secret. The opener correlates outbound frames
// by window source, not by nonce. 'origin' pins the outbound targetOrigin from
// frame zero (otherwise it is learned from the first inbound frame); PR 2 should
// pass 'origin=<dashboard-origin>' as defense in depth. The flasher re-sends
// 'ready' until firmware arrives so a late opener listener cannot wedge the handoff.
//
// EXTENDING THE PROTOCOL: keep changes additive. New optional fields and new
// message types stay forward- and backward-compatible because every receiver
// reads only the fields it knows and ignores unknown message types, and senders
// default absent fields. That is how 'deviceName' was added without a bump.
// Both sides exchange PROTOCOL_VERSION (ReadyMessage.version from the flasher,
// FirmwareMessage.version from the opener); a peer that sees a higher version
// than it speaks should proceed with its known subset (and may warn). Bump
// PROTOCOL_VERSION only for a BREAKING change, and branch on the peer's version
// at that point; additive changes never bump it.

export const PROTOCOL_VERSION = 1;

// Flasher -> opener, announced on load and re-sent until firmware arrives. Carries
// no nonce: the opener identifies us by window source, so this never leaks the secret.
export interface ReadyMessage {
  type: "esphome-web-flash:ready";
  version: number;
}

// One image to write at a flash offset. Bytes ride as a transferable
// ArrayBuffer so the firmware never touches a server.
export interface FlashPart {
  address: number;
  data: ArrayBuffer;
}

// Opener -> flasher, the firmware handoff.
export interface FirmwareMessage {
  type: "esphome-web-flash:firmware";
  nonce: string;
  // The opener's protocol version, so the flasher can branch on it for a future
  // breaking change. Absent means v1.
  version?: number;
  name?: string;
  // The device's friendly name, so the flasher window and tab title identify
  // which device they're for.
  deviceName?: string;
  erase?: boolean;
  parts: FlashPart[];
}

export type FlashState =
  | "connecting"
  | "installing"
  | "done"
  | "error";

// Flasher -> opener, status + progress so the dashboard can mirror it. No nonce
// (see ReadyMessage); the opener correlates by window source.
export interface StateMessage {
  type: "esphome-web-flash:state";
  state: FlashState;
  detail?: string;
}

export interface ProgressMessage {
  type: "esphome-web-flash:progress";
  pct: number;
}

export type OutboundMessage = ReadyMessage | StateMessage | ProgressMessage;
