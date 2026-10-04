// Unit test for the pure log-baud guard in the message contract. esbuild
// transforms the TS module to ESM in memory, as validate.test.mjs does.
import { Buffer } from "node:buffer";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import esbuild from "esbuild";

const src = join(dirname(fileURLToPath(import.meta.url)), "..", "src", "protocol.ts");
const built = await esbuild.build({
  entryPoints: [src],
  bundle: true,
  format: "esm",
  write: false,
});
const { LOG_BAUD_RATE, handoffLogBaudRateOf, logBaudRateFor } = await import(
  "data:text/javascript;base64," + Buffer.from(built.outputFiles[0].text).toString("base64")
);

let ok = true;
const check = (cond, msg) => {
  if (cond) console.log("PASS:", msg);
  else {
    ok = false;
    console.log("FAIL:", msg);
  }
};

check(LOG_BAUD_RATE === 115200, "the default is ESPHome's 115200");

for (const baud of [300, 9600, 115200, 4_000_000]) {
  check(handoffLogBaudRateOf(baud) === baud, `accepts ${baud}`);
}
for (const bad of [undefined, null, 0, 299, 4_000_001, 9600.5, "9600", NaN, Infinity]) {
  check(handoffLogBaudRateOf(bad) === undefined, `ignores ${typeof bad} ${String(bad)}`);
}

check(logBaudRateFor({ logBaudRate: 9600 }) === 9600, "logs open at the baud handed over");
check(logBaudRateFor({}) === 115200, "an opener that did not say gets the default");
check(logBaudRateFor({ logBaudRate: "fast" }) === 115200, "a bad baud gets the default");
check(logBaudRateFor(null) === 115200, "a manually picked file gets the default");

console.log(ok ? "\nALL PASS" : "\nFAILURES");
process.exit(ok ? 0 : 1);
