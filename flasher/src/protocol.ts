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
// at that point; additive changes never bump it. web.esphome.io deploys on its
// own, so every dashboard version must keep working against every receiver in
// both directions; that is what the optional fields' absent-means-v1 defaults are for.

export const PROTOCOL_VERSION = 1;

// Flasher -> opener, announced on load and re-sent until firmware arrives. Carries
// no nonce: the opener identifies us by window source, so this never leaks the secret.
export interface ReadyMessage {
  type: "esphome-web-flash:ready";
  version: number;
  // Flasher ids this receiver has; absent (older receivers) means ['esp'].
  flashers?: HandoffFlasher[];
  // Whether the flasher's browser can actually flash (Web Serial present).
  // The flasher runs on a secure origin, so it can feature-detect for real,
  // unlike a dashboard on plain http. Additive (v1): older flashers omit it,
  // and the opener declines the handoff only on an explicit false; when the
  // field is absent the handoff proceeds and the flasher reports the error
  // itself after the firmware arrives.
  webSerial?: boolean;
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
  // Which flasher writes the parts. Additive (v1): absent means esptool, so
  // an older dashboard's frame is unchanged. An older receiver ignores the
  // field and would write anything as ESP parts, so an opener sends a
  // non-esp id only to a receiver whose ReadyMessage.flashers lists it; that
  // ready-frame gate is the only guard on the receivers already deployed.
  // A receiver refuses a frame whose flasher it did not list, and the id may
  // be one a newer opener knows and the receiver does not.
  // For 'rtl-ambz2', 'rp2-picoboot' and 'bk-uart' the parts are the UF2 as one
  // part at address 0, which the receiver parses into flash runs itself; for
  // 'nrf-dfu' the DFU package the same way, which the receiver unpacks.
  flasher?: HandoffFlasher;
  // Where the device's serial logs are, when the opener knows; absent means
  // elsewhere or unknown.
  logs?: HandoffLogs;
  // The baud the device logs at, when the opener knows it; absent means
  // ESPHome's default. An older receiver ignores it and opens the logs at
  // the default.
  logBaudRate?: number;
  parts: FlashPart[];
}

// ESPHome's default UART log baud.
export const LOG_BAUD_RATE = 115200;

// Plausible UART rates; anything else in the untrusted frame is ignored.
const MIN_LOG_BAUD_RATE = 300;
const MAX_LOG_BAUD_RATE = 4_000_000;

// The inbound 'logBaudRate' field, or undefined for anything that is not a
// plausible baud.
export const handoffLogBaudRateOf = (value: unknown): number | undefined =>
  typeof value === "number" &&
  Number.isInteger(value) &&
  value >= MIN_LOG_BAUD_RATE &&
  value <= MAX_LOG_BAUD_RATE
    ? value
    : undefined;

// 'flash-port': on the port the flash goes over; 'off': the device has none.
export type HandoffLogs = "flash-port" | "off";

// The flasher a hand-off is for, by an id both apps share: 'esp' is esptool
// (ESP32 / ESP8266), 'rtl-ambz2' the RTL8720C ROM downloader, 'rp2-picoboot'
// PICOBOOT for the RP2040, 'nrf-dfu' Nordic legacy DFU for the nRF52, 'bk-uart'
// the UART downloader of a Beken BK72xx. Named after the flasher, not the
// platform: rtl87xx covers the RTL8710B too, whose ROM speaks another protocol
// and gets its own id when it lands.
export type HandoffFlasher =
  | "esp"
  | "rtl-ambz2"
  | "rp2-picoboot"
  | "nrf-dfu"
  | "bk-uart";

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
  // What the user has to do by hand at this point (strap the board into
  // download mode, reset it after the write); the opener shows it in place
  // of its own line. Additive (v1): older flashers omit it and older openers
  // ignore it.
  note?: string;
}

export interface ProgressMessage {
  type: "esphome-web-flash:progress";
  pct: number;
}

export type OutboundMessage = ReadyMessage | StateMessage | ProgressMessage;
