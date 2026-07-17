// The firmware must never leave the browser. That guarantee rests on the CSP
// this page ships, so pin it here: a build that drops or loosens the header
// would otherwise fail silently, and nothing else in CI would notice.
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const here = dirname(fileURLToPath(import.meta.url));
const dist = join(here, "..", "dist");
let ok = true;
const fail = (m) => {
  ok = false;
  console.log("FAIL:", m);
};

// Read from dist/, not the source: the copy the build produces is what gets
// served, and only that copy proves the header survived the build.
const html = readFileSync(join(dist, "index.html"), "utf8");
// The attribute is double-quoted and its value is full of single quotes
// ('none', 'self'), so match on the double quotes only.
const csp = /content="([^"]*default-src[^"]*)"/.exec(html)?.[1] ?? "";

if (!csp) fail("no Content-Security-Policy in the built index.html");
else console.log("PASS: built page ships a CSP");

// connect-src is the one that matters: 'self' lets wasm-bindgen fetch its wasm
// and refuses every other destination, so an ELF cannot be posted anywhere.
if (!/connect-src 'self'(;|$)/.test(csp))
  fail(`connect-src must be exactly 'self', got: ${csp}`);
else console.log("PASS: connect-src 'self' forbids all off-origin requests");

if (!/default-src 'none'/.test(csp)) fail("default-src must be 'none' (closes image beacons)");
else console.log("PASS: default-src 'none'");

// 'wasm-unsafe-eval' is required to compile the module and grants no network
// reach, but 'unsafe-eval' / 'unsafe-inline' in script-src would.
if (/script-src[^;]*'unsafe-eval'/.test(csp)) fail("script-src must not allow 'unsafe-eval'");
else if (/script-src[^;]*'unsafe-inline'/.test(csp))
  fail("script-src must not allow 'unsafe-inline'");
else console.log("PASS: script-src grants only 'self' and 'wasm-unsafe-eval'");

// A third-party reference would both defeat the CSP's point and leak that a
// decode happened. Upstream's own page pulls a CDN stylesheet, which is exactly
// why this page is written here rather than reused from the release.
for (const file of ["index.html", "decoder.js"]) {
  const body = readFileSync(join(dist, file), "utf8");
  const external = (body.match(/https?:\/\/[a-zA-Z0-9./-]+/g) ?? []).filter(
    (u) => !u.startsWith("https://esphome.github.io/"),
  );
  if (external.length) fail(`${file} references third-party URLs: ${external.join(", ")}`);
  else console.log(`PASS: ${file} has no third-party references`);
}

console.log(ok ? "\ncsp: all checks passed" : "\ncsp: FAILED");
process.exit(ok ? 0 : 1);
