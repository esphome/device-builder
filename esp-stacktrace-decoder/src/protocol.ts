// Message contract between the Device Builder dashboard (the embedder, on any
// http/https origin) and this decoder page (a fixed origin on GitHub Pages).
// The embedder origin is unknown, so authentication is the one-time nonce plus
// an "is this my parent" source check, never an origin allowlist. This mirrors
// flasher/src/protocol.ts; read that file's header for the reasoning, which
// applies here unchanged.
//
// The difference from the flasher: this page is embedded in a hidden iframe,
// not opened in a tab. A decode needs neither a user gesture nor a secure
// context, and a crash region arrives mid-log-stream, so a tab per region would
// be unusable. That makes `window.parent` the peer here where the flasher uses
// `window.opener`.
//
// URL hash params: 'nonce' (required) and 'origin' (optional). The nonce is a
// ONE-WAY embedder->decoder token: inbound requests must carry it, but NO
// outbound frame (ready/result/error) ever echoes it, so the pre-handoff 'ready'
// broadcast to '*' leaks no secret. The embedder correlates outbound frames by
// window source, not by nonce. 'origin' pins the outbound targetOrigin from
// frame zero (otherwise it is learned from the first inbound frame). The decoder
// re-sends 'ready' until the first request so a late embedder listener cannot
// wedge the handshake.
//
// EXTENDING THE PROTOCOL: keep changes additive, exactly as the flasher does.
// New optional fields and new message types stay forward- and backward-
// compatible because every receiver reads only the fields it knows and ignores
// unknown message types, and senders default absent fields. Both sides exchange
// PROTOCOL_VERSION (ReadyMessage.version from the decoder, RequestMessage.version
// from the embedder); a peer seeing a higher version than it speaks should
// proceed with its known subset. Bump PROTOCOL_VERSION only for a BREAKING
// change, and branch on the peer's version at that point.

export const PROTOCOL_VERSION = 1;

// Decoder -> embedder, announced on load and re-sent until the first request.
// Carries no nonce: the embedder identifies us by window source.
export interface ReadyMessage {
  type: "esphome-stacktrace-decode:ready";
  version: number;
}

// Embedder -> decoder. One crash region to decode.
//
// The ELF rides as a transferable ArrayBuffer, so it is moved rather than
// copied and never touches a server: this page holds no network egress at all
// (see index.html's CSP). `id` correlates the reply, because one page answers
// many regions over a log session.
export interface RequestMessage {
  type: "esphome-stacktrace-decode:request";
  nonce: string;
  // The embedder's protocol version, so the decoder can branch on it for a
  // future breaking change. Absent means v1.
  version?: number;
  id: string;
  elf: ArrayBuffer;
  // The crash region verbatim. The decoder greps addresses out of it itself,
  // so the caller does not pre-parse.
  dump: string;
}

// One resolved frame. `location` is addr2line's `file:line`, or empty when the
// DWARF has no line for the address.
export interface DecodedFrame {
  address: number;
  function_name: string;
  location: string;
}

// Decoder -> embedder, the answer to one request. An empty `frames` is a
// successful decode that resolved nothing, which is distinct from an error.
export interface ResultMessage {
  type: "esphome-stacktrace-decode:result";
  id: string;
  frames: DecodedFrame[];
}

// Decoder -> embedder. The decode itself failed (unreadable ELF, wasm refused
// to load); the embedder leaves the raw dump alone.
export interface ErrorMessage {
  type: "esphome-stacktrace-decode:error";
  id: string;
  message: string;
}

export type OutboundMessage = ReadyMessage | ResultMessage | ErrorMessage;
