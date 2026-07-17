// Headless check of the postMessage contract this subproject exists to pin,
// plus the claim the whole design rests on: the ELF never leaves the browser.
//
// Decode correctness is upstream's (esp-stacktrace-decoder, on the gimli
// addr2line crate) and is verified against real addr2line output separately.
// What is ours, and what breaks silently, is the handshake, the nonce gate, and
// the egress guarantee.
import http from "node:http";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import puppeteer from "puppeteer";

const DIST = resolve(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const NONCE = "test-nonce-123";
// A real (tiny) ELF with DWARF; see fixtures/README.md for how it was built and
// which addresses it resolves.
const FIXTURE = "tiny-riscv32.elf";
const EMBEDDER = `<!doctype html><meta charset=utf-8><script>
  window.__msgs = [];
  addEventListener('message', (e) => {
    if (window.__f && e.source === window.__f.contentWindow) window.__msgs.push(e.data);
  });
  window.__embed = (url) => {
    const f = document.createElement('iframe');
    f.style.display = 'none';
    f.src = url;
    document.body.appendChild(f);
    window.__f = f;
  };
  window.__send = (msg) => window.__f.contentWindow.postMessage(msg, '*');
</script>embedder`;

const TYPES = { ".js": "text/javascript", ".html": "text/html", ".wasm": "application/wasm" };

// Everything the build produced, by request path. The only files this fixture
// server will ever hand out.
const SERVED = new Map(readdirSync(DIST).map((name) => [`/${name}`, join(DIST, name)]));

const server = http.createServer((req, res) => {
  const path = req.url.split("?")[0].split("#")[0];
  if (path === "/embedder.html") {
    res.writeHead(200, { "content-type": "text/html" });
    return res.end(EMBEDDER);
  }
  // Look the request up in a table built from dist/, rather than turning it
  // into a path. Joining a request path onto a directory serves ../../ straight
  // off the filesystem, and a containment check after the fact is both easy to
  // get subtly wrong and not something CodeQL can see through. A lookup has no
  // path expression to get wrong.
  const file = SERVED.get(path === "/" ? "/index.html" : path);
  if (file === undefined) {
    res.writeHead(404);
    return res.end("nf");
  }
  const ext = file.slice(file.lastIndexOf("."));
  res.writeHead(200, { "content-type": TYPES[ext] ?? "application/octet-stream" });
  res.end(readFileSync(file));
});

await new Promise((r) => server.listen(0, r));
const base = `http://localhost:${server.address().port}`;

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
let ok = true;
const fail = (m) => {
  ok = false;
  console.log("FAIL:", m);
};
const settle = (ms = 300) => new Promise((r) => setTimeout(r, ms));
// Serialize in the page rather than letting puppeteer do it. A BigInt is
// exactly what the decoder must never emit, and puppeteer's serializer answers
// undefined on one, which would turn the clear assertion below into an opaque
// "cannot read properties of undefined" further down. Tag them instead, so the
// failure names the real problem.
const msgs = (p) =>
  p.evaluate(() =>
    JSON.parse(
      JSON.stringify(window.__msgs, (_k, v) => (typeof v === "bigint" ? `bigint:${v}` : v)),
    ),
  );

try {
  const page = await browser.newPage();
  // Every request the browser makes from here on, so the egress claim is
  // measured rather than asserted.
  const requests = [];
  page.on("request", (r) => requests.push(r.url()));
  await page.goto(`${base}/embedder.html`);
  await page.evaluate((u) => window.__embed(u), `${base}/#nonce=${NONCE}`);
  await settle(500);

  // 1. ready reaches the embedder and never echoes the nonce.
  const ready = (await msgs(page)).find((m) => m && m.type === "esphome-stacktrace-decode:ready");
  if (!ready) fail("no ready message received by the embedder");
  else if ("nonce" in ready) fail("ready frame leaked the nonce");
  else if (ready.version !== 1) fail(`ready version mismatch: ${ready.version}`);
  else console.log("PASS: ready received, no nonce echoed, version", ready.version);

  // 1b. ready repeats until the first request: one frame can race the embedder
  // attaching its listener, which would wedge the handshake.
  await page.evaluate(() => {
    window.__msgs.length = 0;
  });
  await settle(700);
  const retried = (await msgs(page)).some((m) => m && m.type === "esphome-stacktrace-decode:ready");
  if (!retried) fail("ready not re-announced before the first request");
  else console.log("PASS: ready re-announced until the first request");

  // 2. a wrong nonce is ignored entirely, with no reply of any kind.
  await page.evaluate(() => {
    window.__msgs.length = 0;
    window.__send({
      type: "esphome-stacktrace-decode:request",
      nonce: "WRONG",
      id: "a",
      elf: new ArrayBuffer(8),
      dump: "Backtrace: 0x400d1a2c:0x3ffb1f00",
    });
  });
  await settle();
  const answered = (await msgs(page)).some(
    (m) => m && (m.type === "esphome-stacktrace-decode:result" || m.type === "esphome-stacktrace-decode:error"),
  );
  if (answered) fail("wrong-nonce request was answered");
  else console.log("PASS: wrong-nonce request ignored");

  // 3. a malformed request is answered rather than left hanging: the embedder
  // has attached and is waiting on the id.
  await page.evaluate(() => {
    window.__msgs.length = 0;
    window.__send({
      type: "esphome-stacktrace-decode:request",
      nonce: "test-nonce-123",
      id: "b",
      elf: "not-a-buffer",
      dump: "Backtrace: 0x400d1a2c:0x3ffb1f00",
    });
  });
  await settle();
  const bad = (await msgs(page)).find((m) => m && m.type === "esphome-stacktrace-decode:error");
  if (!bad) fail("malformed request was not answered with an error");
  else if (bad.id !== "b") fail(`error carried the wrong id: ${bad.id}`);
  else console.log("PASS: malformed request answered with an error, id correlated");

  // 4. a real ELF decodes to real frames. A junk ELF would answer with zero
  // frames, which proves the wasm ran but nothing about the frames themselves;
  // the fixture is here so the assertions below have something to bite on.
  const elf = readFileSync(join(dirname(fileURLToPath(import.meta.url)), "fixtures", FIXTURE));
  await page.evaluate(
    (bytes, dump) => {
      window.__msgs.length = 0;
      window.__send({
        type: "esphome-stacktrace-decode:request",
        nonce: "test-nonce-123",
        id: "c",
        elf: new Uint8Array(bytes).buffer,
        dump,
      });
    },
    [...elf],
    // The shape an esp32c3 panic prints, with the fixture's known addresses.
    "Backtrace: 0x42000020:0x3fc8f000 0x42000040:0x3fc8f020",
  );
  await settle(3000);
  const reply = (await msgs(page)).find((m) => m && m.id === "c");
  if (!reply) fail("real ELF got no reply (wasm failed to load?)");
  else if (reply.type !== "esphome-stacktrace-decode:result")
    fail(`real ELF was not decoded: ${reply.message}`);
  else {
    const got = reply.frames.map((f) => `${f.function_name}@${f.location}`);
    // What real addr2line prints for these addresses; see fixtures/README.md.
    const want = ["crash_here@././tiny.c:3", "main@././tiny.c:4"];
    if (want.some((w) => !got.includes(w)))
      fail(`decoded frames wrong.\n    want superset of ${JSON.stringify(want)}\n    got ${JSON.stringify(got)}`);
    else console.log("PASS: real ELF decoded to the frames addr2line reports");

    // The regression this fixture exists for. Rust types the address u64, so
    // the glue hands back a BigInt; structured clone carries it, so the wire
    // looks fine, and then the embedder's decode cache throws on stringify.
    const bad = reply.frames.filter((f) => typeof f.address !== "number");
    if (bad.length)
      fail(`frame.address must be a number, got ${JSON.stringify(bad[0].address)} (BigInt leaked across postMessage)`);
    else console.log("PASS: frame.address is a plain number");

    // Checked inside the page, on the real frames, because the transport above
    // deliberately launders BigInts to keep failures readable.
    const jsonOk = await page.evaluate(() => {
      try {
        JSON.stringify(window.__msgs);
        return true;
      } catch {
        return false;
      }
    });
    if (!jsonOk) fail("frames are not JSON-serializable; the embedder's decode cache would throw");
    else console.log("PASS: frames are JSON-serializable, so the embedder can cache them");
  }

  const wasmLoaded = requests.some((u) => u.endsWith("_bg.wasm"));
  if (!wasmLoaded) fail("the wasm was never fetched; the decode path is not wired");
  else console.log("PASS: wasm fetched, so the decode path is live");

  // 5. THE claim: nothing left the browser. Every request the page made must be
  // our own origin; an ELF must never appear in one.
  const offOrigin = requests.filter((u) => !u.startsWith(base));
  if (offOrigin.length) fail(`page made off-origin requests: ${offOrigin.join(", ")}`);
  else console.log(`PASS: all ${requests.length} requests were same-origin; nothing was uploaded`);
} finally {
  await browser.close();
  server.close();
}

console.log(ok ? "\nhandshake: all checks passed" : "\nhandshake: FAILED");
process.exit(ok ? 0 : 1);
