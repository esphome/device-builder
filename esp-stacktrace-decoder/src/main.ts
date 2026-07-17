import init, { decode } from "./esp_stacktrace_decoder_rs.js";
import type { DecodedFrame, OutboundMessage, RequestMessage } from "./protocol";
import { PROTOCOL_VERSION } from "./protocol";

const params = new URLSearchParams(location.hash.slice(1));
const nonce = params.get("nonce") ?? "";
// Equal to `window` when someone opens the page directly, which is not an
// embedder and must never be posted to.
const peer: Window | null = window.parent === window ? null : window.parent;

// Where outbound frames are sent. The embedder may pin its origin in the hash;
// otherwise this stays '*' until the first valid inbound frame reveals
// ev.origin. Outbound frames carry no nonce (see protocol.ts), so the
// pre-handoff '*' fallback leaks no secret.
let targetOrigin = params.get("origin") || "*";

function post(msg: OutboundMessage): void {
  try {
    peer?.postMessage(msg, targetOrigin);
  } catch (err) {
    // A malformed origin= hash param (e.g. origin=null) makes postMessage
    // throw, which would wedge the handshake. `peer` is one fixed window
    // either way, so '*' drops the origin assertion, not the audience.
    console.error("Decoder postMessage failed; falling back to '*':", err);
    targetOrigin = "*";
    try {
      peer?.postMessage(msg, "*");
    } catch (err2) {
      console.error("Decoder postMessage failed after origin fallback:", err2);
    }
  }
}

// Memoized: instantiating the wasm costs ~1MB of compile, and a crash loop asks
// for many regions over one session.
let ready: Promise<unknown> | undefined;
function wasm(): Promise<unknown> {
  if (ready === undefined) {
    ready = init().catch((err) => {
      // Memoize the success, not the failure: one transient fetch error would
      // otherwise mean raw dumps for the rest of the session.
      ready = undefined;
      throw err;
    });
  }
  return ready;
}

/**
 * Decode one region, converting the wasm objects to plain frames.
 *
 * The DecodedAddress instances are wasm-backed handles, which structured clone
 * cannot carry, so every field is read out here. They are freed eagerly rather
 * than left to the FinalizationRegistry: a crash loop decodes the same region
 * repeatedly, and the ELF's arena is large enough that GC timing shows.
 */
async function decodeRegion(elf: ArrayBuffer, dump: string): Promise<DecodedFrame[]> {
  await wasm();
  const decoded = decode(new Uint8Array(elf), dump);
  const frames: DecodedFrame[] = [];
  try {
    for (const entry of decoded) {
      frames.push({
        // A BigInt (Rust u64). Structured clone would carry it, but the
        // embedder caches decodes and JSON.stringify throws on one. An ESP
        // address is 32 bits, so narrowing is lossless.
        address: Number(entry.address),
        function_name: entry.function_name,
        location: entry.location,
      });
    }
  } finally {
    // Guarded individually so one bad handle can't abandon the rest, or mask
    // the in-flight decode error.
    for (const entry of decoded) {
      try {
        entry.free();
      } catch (err) {
        console.warn("Freeing a decoded frame failed", err);
      }
    }
  }
  return frames;
}

window.addEventListener("message", (ev: MessageEvent) => {
  // Only accept work from the window that framed us, and only when the nonce
  // matches. No origin allowlist is possible: the dashboard runs on an
  // arbitrary (often http) origin.
  if (!peer || ev.source !== peer) return;
  const data = ev.data as Partial<RequestMessage> | undefined;
  if (!data || data.type !== "esphome-stacktrace-decode:request") return;
  // Fail closed when framed without a nonce, rather than letting the gate
  // degrade to "" === "" and accept any request carrying an empty one.
  if (!nonce || data.nonce !== nonce) return;
  // The embedder origin is now known; stop broadcasting and pin to it.
  if (targetOrigin === "*" && ev.origin && ev.origin !== "null") {
    targetOrigin = ev.origin;
  }
  stopReadyRetry();
  const id = typeof data.id === "string" ? data.id : "";
  if (!id) {
    // An error frame is correlated by id, so answering this one would be
    // unroutable. Announce instead: it needs no id, and the embedder is left
    // with something to log rather than a timeout.
    post({
      type: "esphome-stacktrace-decode:unavailable",
      reason: "Decode request carried no id, so its reply could not be routed.",
    });
    return;
  }
  if (!(data.elf instanceof ArrayBuffer) || typeof data.dump !== "string") {
    post({ type: "esphome-stacktrace-decode:error", id, message: "Malformed decode request" });
    return;
  }
  const { elf, dump } = data;
  decodeRegion(elf, dump).then(
    (frames) => post({ type: "esphome-stacktrace-decode:result", id, frames }),
    (err) =>
      post({
        type: "esphome-stacktrace-decode:error",
        id,
        message: String(err),
      }),
  );
});

// Re-announce until the first request: a single 'ready' can race the embedder
// attaching its message listener after creating the iframe, wedging the
// handshake. Same reasoning as the flasher's ready retry.
let readyTimer: number | undefined;

function stopReadyRetry(): void {
  if (readyTimer !== undefined) {
    clearInterval(readyTimer);
    readyTimer = undefined;
  }
}

function sendReady(): void {
  post({ type: "esphome-stacktrace-decode:ready", version: PROTOCOL_VERSION });
}

if (peer && !nonce) {
  const reason =
    "Framed without a nonce, so no decode can be authorized. The embedder must " +
    "frame this page as .../#nonce=<random>&origin=<its-origin>.";
  console.error(reason);
  post({ type: "esphome-stacktrace-decode:unavailable", reason });
} else if (peer) {
  sendReady();
  let waited = 0;
  readyTimer = window.setInterval(() => {
    waited += 500;
    // Stop rather than announce: the embedder gives up on the same 10s clock
    // and drops both its listener and this frame, so there is nobody left to
    // tell, and nothing re-frames.
    if (waited >= 10000) {
      stopReadyRetry();
      return;
    }
    sendReady();
  }, 500);
}
