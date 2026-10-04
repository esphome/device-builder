// Unit tests for the pure image-magic validator and hand-off guards. esbuild transforms the TS
// module to ESM in memory so it can be imported without the DOM-touching entry.
import { Buffer } from "node:buffer";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import esbuild from "esbuild";

const load = async (name) => {
  const src = join(dirname(fileURLToPath(import.meta.url)), "..", "src", name);
  const built = await esbuild.build({ entryPoints: [src], bundle: true, format: "esm", write: false });
  return import(
    "data:text/javascript;base64," + Buffer.from(built.outputFiles[0].text).toString("base64")
  );
};
const { validateEspImage } = await load("image-magic.ts");
const { handoffLogBaudRateOf } = await load("protocol.ts");

let ok = true;
const check = (cond, msg) => {
  if (cond) console.log("PASS:", msg);
  else {
    ok = false;
    console.log("FAIL:", msg);
  }
};

// ESP8266 / native-USB ESP32 (S3/C3/C6): magic at byte 0.
check(
  validateEspImage([{ address: 0, data: new Uint8Array([0xe9, 0, 0, 0]) }]) === null,
  "accepts 0xE9 at byte 0",
);

// Original ESP32 / ESP32-S2 merged factory: 0xFF pad, bootloader magic at 0x1000.
const esp32 = new Uint8Array(0x1001).fill(0xff);
esp32[0x1000] = 0xe9;
check(validateEspImage([{ address: 0, data: esp32 }]) === null, "accepts 0xE9 at 0x1000 (padded esp32)");

// rp2040 .uf2 ("UF2\n") has no ESP magic at either offset.
const uf2 = new Uint8Array([0x55, 0x46, 0x32, 0x0a]);
check(validateEspImage([{ address: 0, data: uf2 }]) !== null, "rejects a uf2 image");

// 0xFF-padded but no magic at 0x1000 either.
check(
  validateEspImage([{ address: 0, data: new Uint8Array(0x2000) }]) !== null,
  "rejects an all-zero buffer",
);

// No part at offset 0.
check(
  validateEspImage([{ address: 0x10000, data: new Uint8Array([0xe9]) }]) !== null,
  "rejects when no image at 0x0",
);

// The hand-off's log baud: plausible rates pass, anything else is dropped.
for (const baud of [300, 9600, 115200, 4_000_000]) {
  check(handoffLogBaudRateOf(baud) === baud, `log baud accepts ${baud}`);
}
for (const bad of [undefined, null, 0, 299, 4_000_001, 9600.5, "9600", NaN, Infinity]) {
  check(handoffLogBaudRateOf(bad) === undefined, `log baud ignores ${typeof bad} ${String(bad)}`);
}

console.log(ok ? "\nALL PASS" : "\nFAILURES");
process.exit(ok ? 0 : 1);
