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

let firmware: FirmwareMessage | null = null;
let busy = false;

// --- UI -------------------------------------------------------------------

document.body.innerHTML = `
  <main>
    <h1>ESPHome Device Builder USB flasher</h1>
    <p id="hint"></p>
    <div id="status" class="status idle">Waiting for firmware&hellip;</div>
    <div id="bar" hidden><div id="fill"></div></div>
    <button id="install" disabled>Connect &amp; install</button>
    <details id="manual">
      <summary>No firmware received? Flash a factory file manually</summary>
      <input id="file" type="file" accept=".bin" />
    </details>
    <pre id="log"></pre>
  </main>
`;

const el = <T extends HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const statusEl = el<HTMLDivElement>("status");
const barEl = el<HTMLDivElement>("bar");
const fillEl = el<HTMLDivElement>("fill");
const installBtn = el<HTMLButtonElement>("install");
const fileInput = el<HTMLInputElement>("file");
const hintEl = el<HTMLParagraphElement>("hint");
const logEl = el<HTMLPreElement>("log");

hintEl.textContent = opener
  ? "Plug the board into this computer over USB, then press Connect & install."
  : "Opened directly: pick a factory .bin below, plug in the board, then press Connect & install.";

function log(line: string): void {
  logEl.textContent += line + "\n";
}

function post(msg: OutboundMessage): void {
  // Opener origin is unknown; status carries no secrets, so target '*'. The
  // nonce scopes the message to this session.
  opener?.postMessage(msg, "*");
}

function setState(state: FlashState, detail: string): void {
  statusEl.textContent = detail;
  statusEl.className = "status " + state;
  if (nonce) {
    post({ type: "esphome-web-flash:state", nonce, state, detail });
  }
}

function setProgress(pct: number): void {
  barEl.hidden = false;
  fillEl.style.width = pct + "%";
  if (nonce) post({ type: "esphome-web-flash:progress", nonce, pct });
}

// --- handoff --------------------------------------------------------------

window.addEventListener("message", (ev: MessageEvent) => {
  // Only accept the firmware from the window that opened us, and only when the
  // nonce matches. No origin allowlist is possible since the dashboard runs on
  // an arbitrary (often http) origin.
  if (!opener || ev.source !== opener) return;
  const data = ev.data as Partial<FirmwareMessage> | undefined;
  if (!data || data.type !== "esphome-web-flash:firmware") return;
  if (data.nonce !== nonce) return;
  if (!Array.isArray(data.parts) || data.parts.length === 0) return;
  firmware = data as FirmwareMessage;
  installBtn.disabled = busy;
  setState("connecting", `Firmware ready${firmware.name ? ": " + firmware.name : ""}. Press Connect & install.`);
});

if (opener && nonce) {
  const ready: OutboundMessage = {
    type: "esphome-web-flash:ready",
    version: PROTOCOL_VERSION,
    nonce,
  };
  opener.postMessage(ready, "*");
}

// --- flashing (mirrors esphome/dashboard src/web-serial) ------------------

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));


async function flashFiles(
  esploader: ESPLoader,
  fileArray: FileToFlash[],
  erase: boolean,
  writeProgress: (pct: number) => void,
): Promise<void> {
  if (erase) {
    await esploader.eraseFlash();
  }
  let totalSize = 0;
  for (const f of fileArray) totalSize += f.data.length;
  let totalWritten = 0;
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

async function runFlash(files: FileToFlash[], erase: boolean): Promise<void> {
  if (busy) return;
  busy = true;
  installBtn.disabled = true;
  fileInput.disabled = true;

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

    setState("installing", "Installing… keep this tab visible.");
    await flashFiles(esploader, files, erase, setProgress);
    await esploader.after();
    await resetDevice(transport);
    setState("done", "Installed. The device is rebooting.");
  } catch (err) {
    setState("error", "Installation failed: " + String(err));
  } finally {
    try {
      await transport.disconnect();
    } catch {
      // already closed
    }
    busy = false;
    installBtn.disabled = false;
    fileInput.disabled = false;
  }
}

installBtn.addEventListener("click", async () => {
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
