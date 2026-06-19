import { ESPLoader, Transport } from "esptool-js";
import type {
  FirmwareMessage,
  OutboundMessage,
  FlashState,
} from "./protocol";
import { PROTOCOL_VERSION } from "./protocol";

// One image to write, in the byte form esptool-js 0.6 expects.
interface FileToFlash {
  data: Uint8Array;
  address: number;
}

const params = new URLSearchParams(location.hash.slice(1));
const nonce = params.get("nonce") ?? "";
const opener = window.opener as Window | null;

// Where outbound frames are sent. The opener may pin its origin in the hash;
// otherwise this stays '*' until the first valid inbound frame reveals ev.origin,
// after which we target that. Outbound frames carry no nonce (see protocol.ts),
// so the pre-handoff '*' fallback leaks no secret; '#origin=' pins it from the
// start as defense in depth.
let targetOrigin = params.get("origin") || "*";

let firmware: FirmwareMessage | null = null;
let busy = false;
let flashDone = false;
let streaming = false;
let stopLogs = false;

// --- UI -------------------------------------------------------------------

document.body.innerHTML = `
  <main>
    <h1>ESPHome Device Builder USB flasher</h1>
    <div class="devbanner">
      <strong>Development build, for testing only.</strong> The production
      flasher is <a href="https://web.esphome.io/">web.esphome.io</a>. This page
      exists only to develop and verify the dashboard flashing flow before that
      capability is added to the production site. Do not rely on it.
    </div>
    <p id="hint"></p>
    <div id="status" class="status idle">Waiting for firmware&hellip;</div>
    <div id="progress" hidden><div id="bar"><div id="fill"></div></div><span id="pct"></span></div>
    <button id="install" disabled>Connect &amp; install</button>
    <details id="manual">
      <summary>No firmware received? Flash a factory file manually</summary>
      <input id="file" type="file" accept=".bin" />
    </details>
    <details id="logbox">
      <summary>Show log</summary>
      <pre id="log"></pre>
    </details>
  </main>
`;

const el = <T extends HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const statusEl = el<HTMLDivElement>("status");
const progressEl = el<HTMLDivElement>("progress");
const fillEl = el<HTMLDivElement>("fill");
const pctEl = el<HTMLSpanElement>("pct");
const installBtn = el<HTMLButtonElement>("install");
const fileInput = el<HTMLInputElement>("file");
const hintEl = el<HTMLParagraphElement>("hint");
const logEl = el<HTMLPreElement>("log");
const logbox = el<HTMLDetailsElement>("logbox");

// CSI escapes (colour, cursor moves) in ESPHome's serial log; strip them so
// the log shows plain text instead of raw control sequences.
const ANSI_RE = /\[[\d;?]*[A-Za-z]/g;

hintEl.textContent = opener
  ? "Plug the board into this computer over USB, then press Connect & install."
  : "Opened directly: pick a factory .bin below, plug in the board, then press Connect & install.";

function log(line: string): void {
  logEl.textContent += line + "\n";
  logEl.scrollTop = logEl.scrollHeight;
}

// esptool-js streams its output here; a \r redraws the current line (progress),
// so keep only what follows the last \r when flushing a completed line.
let pending = "";
function termWrite(data: string): void {
  pending += data;
  let nl = pending.indexOf("\n");
  while (nl >= 0) {
    log(pending.slice(0, nl).replace(/.*\r/, "").replace(ANSI_RE, ""));
    pending = pending.slice(nl + 1);
    nl = pending.indexOf("\n");
  }
}

const terminal = {
  clean(): void {
    logEl.textContent = "";
    pending = "";
  },
  writeLine(data: string): void {
    log(data);
  },
  write(data: string): void {
    termWrite(data);
  },
};

function post(msg: OutboundMessage): void {
  // targetOrigin narrows from '*' to the opener's real origin once known; see
  // its declaration. Outbound frames carry no nonce (see protocol.ts).
  opener?.postMessage(msg, targetOrigin);
}

function setState(state: FlashState, detail: string): void {
  statusEl.className = "status " + state;
  statusEl.textContent = "";
  if (state === "connecting" || state === "installing") {
    const spin = document.createElement("span");
    spin.className = "spinner";
    statusEl.appendChild(spin);
  }
  statusEl.appendChild(document.createTextNode(detail));
  post({ type: "esphome-web-flash:state", state, detail });
}

function setProgress(pct: number): void {
  progressEl.hidden = false;
  fillEl.style.width = pct + "%";
  pctEl.textContent = pct + "%";
  post({ type: "esphome-web-flash:progress", pct });
}

// --- handoff --------------------------------------------------------------

function isFlashParts(parts: unknown): parts is FirmwareMessage["parts"] {
  return (
    Array.isArray(parts) &&
    parts.length > 0 &&
    parts.every((p) => {
      if (!p || typeof p !== "object") return false;
      const address = (p as { address?: unknown }).address;
      return (
        typeof address === "number" &&
        Number.isInteger(address) &&
        address >= 0 &&
        (p as { data?: unknown }).data instanceof ArrayBuffer
      );
    })
  );
}

window.addEventListener("message", (ev: MessageEvent) => {
  // Only accept the firmware from the window that opened us, and only when the
  // nonce matches. No origin allowlist is possible since the dashboard runs on
  // an arbitrary (often http) origin.
  if (!opener || ev.source !== opener) return;
  const data = ev.data as Partial<FirmwareMessage> | undefined;
  if (!data || data.type !== "esphome-web-flash:firmware") return;
  if (data.nonce !== nonce) return;
  if (!isFlashParts(data.parts)) {
    setState("error", "Received a malformed firmware payload.");
    return;
  }
  // The opener origin is now known; stop broadcasting and pin to it.
  if (targetOrigin === "*" && ev.origin && ev.origin !== "null") {
    targetOrigin = ev.origin;
  }
  stopReadyRetry();
  firmware = data as FirmwareMessage;
  installBtn.disabled = busy;
  setState(
    "connecting",
    `Firmware ready${firmware.name ? ": " + firmware.name : ""}. Press Connect & install.`,
  );
  installBtn.focus();
});

// Re-announce until firmware arrives: a single 'ready' can race the opener
// attaching its message listener after window.open(), wedging the handoff.
let readyTimer: number | undefined;

function stopReadyRetry(): void {
  if (readyTimer !== undefined) {
    clearInterval(readyTimer);
    readyTimer = undefined;
  }
}

function sendReady(): void {
  post({ type: "esphome-web-flash:ready", version: PROTOCOL_VERSION });
}

if (opener && nonce) {
  sendReady();
  let waited = 0;
  readyTimer = window.setInterval(() => {
    waited += 500;
    if (firmware || waited >= 10000) {
      stopReadyRetry();
      return;
    }
    sendReady();
  }, 500);
}

// --- flashing (mirrors esphome/dashboard src/web-serial) ------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));


async function flashFiles(
  esploader: ESPLoader,
  fileArray: FileToFlash[],
  erase: boolean,
  writeProgress: (pct: number) => void,
  setPhase: (detail: string) => void,
): Promise<void> {
  if (erase) {
    setPhase("Erasing flash… this can take a moment.");
    await esploader.eraseFlash();
  }
  let totalSize = 0;
  for (const f of fileArray) totalSize += f.data.length;
  let totalWritten = 0;
  setPhase("Writing firmware… keep this tab visible.");
  writeProgress(0);
  await esploader.writeFlash({
    fileArray,
    flashSize: "keep",
    flashMode: "keep",
    flashFreq: "keep",
    eraseAll: false,
    compress: true,
    reportProgress: (fileIndex: number, written: number, total: number) => {
      const uncompressedWritten =
        (written / total) * fileArray[fileIndex].data.length;
      const pct = Math.floor(
        ((totalWritten + uncompressedWritten) / totalSize) * 100,
      );
      if (written === total) {
        totalWritten += uncompressedWritten;
        return;
      }
      writeProgress(pct);
    },
  });
  writeProgress(100);
}

async function resetDevice(transport: Transport): Promise<void> {
  await transport.setRTS(true); // EN -> LOW
  await sleep(100);
  await transport.setRTS(false);
}

// Stream the rebooted device's serial output into the log so a tester can watch
// the boot over the same port that flashed it. Reads at the flash baud (115200,
// the ESPHome logger default); a native-USB chip that re-enumerated on reset may
// end the read early, which is surfaced rather than thrown.
async function streamSerialLogs(transport: Transport): Promise<void> {
  streaming = true;
  logbox.open = true;
  installBtn.textContent = "Stop logs";
  installBtn.disabled = false;
  const decoder = new TextDecoder();
  try {
    await transport.rawRead(
      (chunk) => termWrite(decoder.decode(chunk, { stream: true })),
      () => stopLogs,
    );
  } catch (err) {
    log("[serial logs unavailable: " + String(err) + "]");
  } finally {
    streaming = false;
  }
}

async function runFlash(files: FileToFlash[], erase: boolean): Promise<void> {
  if (busy) return;
  busy = true;
  installBtn.disabled = true;
  fileInput.disabled = true;
  // Clear any stale bar/percent from a previous failed attempt.
  stopLogs = false;
  progressEl.hidden = true;
  fillEl.style.width = "0%";
  pctEl.textContent = "";

  let port: SerialPort;
  try {
    port = await navigator.serial.requestPort();
  } catch {
    setState("error", "No serial port selected.");
    busy = false;
    installBtn.disabled = false;
    fileInput.disabled = false;
    return;
  }

  const transport = new Transport(port);
  const esploader = new ESPLoader({
    transport,
    baudrate: 115200,
    enableTracing: false,
    terminal,
  });

  try {
    setState("connecting", "Connecting to device…");
    let chipName: string;
    try {
      chipName = await esploader.main();
      await esploader.flashId();
    } catch (err) {
      console.error(err);
      setState(
        "error",
        "Failed to initialize. Reset the board, or hold BOOT while selecting the port, then retry.",
      );
      return;
    }
    log("Detected chip: " + chipName);

    await flashFiles(esploader, files, erase, setProgress, (detail) =>
      setState("installing", detail),
    );
    await esploader.after();
    await resetDevice(transport);
    flashDone = true;
    setState(
      "done",
      opener
        ? "Installed and rebooting. Live serial logs below; close this tab when finished."
        : "Installed and rebooting. Live serial logs below; press Stop logs when finished.",
    );
    await streamSerialLogs(transport);
  } catch (err) {
    setState("error", "Installation failed: " + String(err));
  } finally {
    try {
      await transport.disconnect();
    } catch {
      // already closed
    }
    busy = false;
    fileInput.disabled = false;
    // Only offer the self-close action when an opener exists; window.close() is
    // blocked on a tab the user navigated to directly.
    if (flashDone && opener) {
      installBtn.textContent = "Close this tab";
      installBtn.disabled = false;
    } else if (flashDone) {
      installBtn.textContent = "Done";
      installBtn.disabled = true;
    } else {
      installBtn.disabled = false;
    }
  }
}

installBtn.addEventListener("click", async () => {
  if (streaming) {
    stopLogs = true; // end the live-log read; the run's finally takes over
    return;
  }
  if (flashDone) {
    window.close();
    return;
  }
  if (firmware) {
    const files = firmware.parts.map((p) => ({
      data: new Uint8Array(p.data),
      address: p.address,
    }));
    await runFlash(files, firmware.erase ?? true);
    return;
  }
  const file = fileInput.files?.[0];
  if (!file) {
    setState("error", "Choose a factory .bin file first.");
    return;
  }
  const data = new Uint8Array(await file.arrayBuffer());
  await runFlash([{ data, address: 0 }], true);
});

fileInput.addEventListener("change", () => {
  installBtn.disabled = busy || !fileInput.files?.length;
});

if (!("serial" in navigator)) {
  setState(
    "error",
    "This browser has no Web Serial. Use Chrome or Edge over https or 127.0.0.1.",
  );
  installBtn.disabled = true;
}
