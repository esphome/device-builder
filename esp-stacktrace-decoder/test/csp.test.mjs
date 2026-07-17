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

// Pinned exactly, like connect-src, rather than by testing for the absence of
// a couple of bad tokens: `script-src 'self' 'wasm-unsafe-eval' data: blob:`
// passes any absence check while handing back a script-injection foothold, and
// the URL scan below can't see it either (data:/blob: have no scheme-slash
// form). 'wasm-unsafe-eval' is what lets the module compile; it grants no
// network reach.
if (!/script-src 'self' 'wasm-unsafe-eval'(;|$)/.test(csp))
  fail(`script-src must be exactly 'self' 'wasm-unsafe-eval', got: ${csp}`);
else console.log("PASS: script-src grants only 'self' and 'wasm-unsafe-eval'");

// Also part of the shipped header and the README's guarantee, so also pinned.
for (const directive of ["form-action 'none'", "base-uri 'none'"]) {
  if (!csp.includes(directive)) fail(`CSP must carry ${directive}, got: ${csp}`);
  else console.log(`PASS: ${directive}`);
}

// A third-party reference would both defeat the CSP's point and leak that a
// decode happened. Upstream's own page pulls a CDN stylesheet, which is exactly
// why this page is written here rather than reused from the release; the glue
// is scanned too, because it is the only third-party code that ships, so it is
// the one file this check was written for. If a bump makes it carry a URL
// legitimately, that is worth learning here rather than in production.
for (const file of ["index.html", "decoder.js", "esp_stacktrace_decoder_rs.js"]) {
  const body = readFileSync(join(dist, file), "utf8");
  const external = (body.match(/https?:\/\/[a-zA-Z0-9./-]+/g) ?? []).filter(
    (u) => !u.startsWith("https://esphome.github.io/"),
  );
  if (external.length) fail(`${file} references third-party URLs: ${external.join(", ")}`);
  else console.log(`PASS: ${file} has no third-party references`);
}

console.log(ok ? "\ncsp: all checks passed" : "\ncsp: FAILED");
process.exit(ok ? 0 : 1);
