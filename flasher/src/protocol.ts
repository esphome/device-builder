// Message contract between the Device Builder dashboard (the opener, on any
// http/https origin) and this flasher page (a fixed secure-context origin).
// The opener origin is unknown, so authentication is the one-time nonce plus an
// "is this my opener" source check, never an origin allowlist. This same
// contract is what PR 2 reimplements inside web.esphome.io.
//
// URL hash params the flasher reads: 'nonce' (required, the session secret) and
// 'origin'. 'origin' pins the outbound targetOrigin; without it, outbound stays
// '*' until the first inbound frame reveals the origin, which means the early
// 'ready' frames broadcast the nonce to '*'. The production integration (PR 2)
// MUST pass 'origin=<dashboard-origin>' so 'ready' never broadcasts. The flasher
// re-sends 'ready' until firmware arrives so a late opener listener cannot wedge
// the handoff.

export const PROTOCOL_VERSION = 1;

// Flasher -> opener, once on load.
export interface ReadyMessage {
  type: "esphome-web-flash:ready";
  version: number;
  nonce: string;
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
  name?: string;
  erase?: boolean;
  parts: FlashPart[];
}

export type FlashState =
  | "connecting"
  | "installing"
  | "done"
  | "error";

// Flasher -> opener, status + progress so the dashboard can mirror it.
export interface StateMessage {
  type: "esphome-web-flash:state";
  nonce: string;
  state: FlashState;
  detail?: string;
}

export interface ProgressMessage {
  type: "esphome-web-flash:progress";
  nonce: string;
  pct: number;
}

export type OutboundMessage = ReadyMessage | StateMessage | ProgressMessage;
