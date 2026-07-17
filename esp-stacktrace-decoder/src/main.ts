import init, { decode } from "./esp_stacktrace_decoder_rs.js";
import type { DecodedFrame, OutboundMessage, RequestMessage } from "./protocol";
import { PROTOCOL_VERSION } from "./protocol";

const params = new URLSearchParams(location.hash.slice(1));
const nonce = params.get("nonce") ?? "";
// The embedder. Unlike the flasher (a tab, so window.opener), this page is
// framed, so the peer is the parent. Equal to `window` when someone opens the
// page directly, which is not an embedder and must never be posted to.
const peer: Window | null = window.parent === window ? null : window.parent;

// Where outbound frames are sent. The embedder may pin its origin in the hash;
// otherwise this stays '*' until the first valid inbound frame reveals
// ev.origin. Outbound frames carry no nonce (see protocol.ts), so the
// pre-handoff '*' fallback leaks no secret.
let targetOrigin = params.get("origin") || "*";
let requested = false;

function post(msg: OutboundMessage): void {
  try {
    peer?.postMessage(msg, targetOrigin);
  } catch (err) {
    // A malformed origin= hash param (e.g. origin=null) makes postMessage
    // throw, which would wedge the ready handshake. Fall back to '*' so frames
    // keep flowing; outbound frames carry no nonce, so the broader audience
    // leaks nothing. Log rather than swallow so an unrelated failure is still
    // visible.
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
      // Only the success is worth keeping. Latching a rejection would turn one
      // transient fetch failure into raw dumps for the rest of the session,
      // which is not what memoizing a ~1MB compile is for.
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
        // Rust types the address u64, so the glue hands back a BigInt. Narrow
        // it here: structured clone would carry a BigInt happily, but
        // JSON.stringify throws on one, and the embedder caches decodes. An ESP
        // address is 32 bits, so this is lossless.
        address: Number(entry.address),
        function_name: entry.function_name,
        location: entry.location,
      });
    }
  } finally {
    // In a finally because the eager free exists for the crash loop, which is
    // also where a malformed region is most likely to throw partway through;
    // leaving the rest to the FinalizationRegistry there is the one case it was
    // meant to avoid. Double-freeing is not a risk: each handle is freed once.
    for (const entry of decoded) entry.free();
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
  // Fail closed when we were framed without a nonce, rather than letting the
  // gate degrade to "" === "" and accept any request carrying an empty one.
  // Defence in depth (ev.source already restricts this to whoever framed us),
  // but an auth check that quietly becomes a no-op is worse than no check: it
  // still reads like one.
  if (!nonce || data.nonce !== nonce) return;
  // The embedder origin is now known; stop broadcasting and pin to it.
  if (targetOrigin === "*" && ev.origin && ev.origin !== "null") {
    targetOrigin = ev.origin;
  }
  requested = true;
  stopReadyRetry();
  const id = typeof data.id === "string" ? data.id : "";
  if (!(data.elf instanceof ArrayBuffer) || typeof data.dump !== "string" || !id) {
    // The embedder has attached and sent, so the handshake is over even though
    // this payload is unusable. Answer so it isn't left waiting on a timeout.
    post({
      type: "esphome-stacktrace-decode:error",
      id,
      message: "Malformed decode request",
    });
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
  // Say so. Without this, a framed page missing its nonce just never answers,
  // which the embedder cannot tell apart from the page being unreachable, so a
  // wiring mistake reads as an outage and every crash silently stays raw.
  console.error(
    "Framed without a nonce, so no decode can be authorized. The embedder must " +
      "frame this page as .../#nonce=<random>&origin=<its-origin>.",
  );
} else if (peer && nonce) {
  sendReady();
  let waited = 0;
  readyTimer = window.setInterval(() => {
    waited += 500;
    if (requested || waited >= 10000) {
      stopReadyRetry();
      return;
    }
    sendReady();
  }, 500);
}
