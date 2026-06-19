// Headless check of the postMessage contract this subproject exists to pin.
// Web Serial flashing itself needs real hardware and is not covered here.
import http from "node:http";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import puppeteer from "puppeteer";

const DIST = join(dirname(fileURLToPath(import.meta.url)), "..", "dist");
const OPENER = `<!doctype html><meta charset=utf-8><script>
  window.__msgs = [];
  addEventListener('message', (e) => window.__msgs.push(e.data));
  window.__open = (url) => { window.__b = window.open(url); };
</script>opener`;

const server = http.createServer((req, res) => {
  const path = req.url.split("?")[0];
  if (path === "/opener.html") {
    res.writeHead(200, { "content-type": "text/html" });
    return res.end(OPENER);
  }
  const file = path === "/" ? "/index.html" : path;
  try {
    const body = readFileSync(DIST + file);
    const ct = file.endsWith(".js")
      ? "text/javascript"
      : file.endsWith(".html")
        ? "text/html"
        : "application/octet-stream";
    res.writeHead(200, { "content-type": ct });
    res.end(body);
  } catch {
    res.writeHead(404);
    res.end("nf");
  }
});

await new Promise((r) => server.listen(0, r));
const base = `http://localhost:${server.address().port}`;

const browser = await puppeteer.launch({ args: ["--no-sandbox"] });
let ok = true;
const fail = (m) => {
  ok = false;
  console.log("FAIL:", m);
};

try {
  const a = await browser.newPage();
  await a.goto(`${base}/opener.html`);

  const flasherUrl = `${base}/#nonce=test-nonce-123`;
  const [popup] = await Promise.all([
    new Promise((res) => a.once("popup", res)),
    a.evaluate((u) => window.__open(u), flasherUrl),
  ]);
  await popup.waitForNetworkIdle({ idleTime: 300 }).catch(() => {});
  await new Promise((r) => setTimeout(r, 300));

  // 1. flasher posts a ready frame to its opener that does NOT echo the nonce
  const ready = (await a.evaluate(() => window.__msgs)).find(
    (m) => m && m.type === "esphome-web-flash:ready",
  );
  if (!ready) fail("no ready message received by opener");
  else if ("nonce" in ready) fail("ready frame leaked the nonce");
  else if (ready.version !== 1) fail("ready version mismatch");
  else console.log("PASS: ready received, no nonce echoed, version", ready.version);

  // 1b. ready is re-announced until firmware arrives (handshake robustness)
  await a.evaluate(() => {
    window.__msgs.length = 0;
  });
  await new Promise((r) => setTimeout(r, 700));
  const retried = (await a.evaluate(() => window.__msgs)).some(
    (m) => m && m.type === "esphome-web-flash:ready",
  );
  if (!retried) fail("ready not re-announced before firmware");
  else console.log("PASS: ready re-announced until firmware arrives");

  // 2. wrong nonce must be ignored
  await a.evaluate(() => {
    window.__b.postMessage(
      {
        type: "esphome-web-flash:firmware",
        nonce: "WRONG",
        name: "bad",
        parts: [{ address: 0, data: new ArrayBuffer(8) }],
      },
      "*",
    );
  });
  await new Promise((r) => setTimeout(r, 200));
  let label = await popup.$eval("#status", (e) => e.textContent);
  if (/firmware ready/i.test(label)) fail("wrong-nonce firmware was accepted");
  else console.log("PASS: wrong-nonce firmware ignored");

  // 2b. malformed parts (data not an ArrayBuffer) -> error state, not a throw
  await a.evaluate(() => {
    window.__b.postMessage(
      {
        type: "esphome-web-flash:firmware",
        nonce: "test-nonce-123",
        name: "bad",
        parts: [{ address: 0, data: "not-a-buffer" }],
      },
      "*",
    );
  });
  await new Promise((r) => setTimeout(r, 200));
  label = await popup.$eval("#status", (e) => e.textContent);
  const enabledAfterBad = await popup.$eval("#install", (b) => !b.disabled);
  if (!/malformed/i.test(label))
    fail("malformed payload not reported: " + label);
  else if (enabledAfterBad) fail("install enabled after malformed payload");
  else console.log("PASS: malformed payload rejected with error state");

  // 2b-ii. ...and the malformed payload stops the ready re-announce: the opener
  // has clearly attached, so it must not keep receiving ready frames.
  await a.evaluate(() => {
    window.__msgs.length = 0;
  });
  await new Promise((r) => setTimeout(r, 700));
  const readyAfterBad = (await a.evaluate(() => window.__msgs)).some(
    (m) => m && m.type === "esphome-web-flash:ready",
  );
  if (readyAfterBad) fail("ready still re-announced after malformed payload");
  else console.log("PASS: ready retry stopped after malformed payload");

  // 2c. a negative/non-integer address is rejected at the boundary
  await a.evaluate(() => {
    window.__b.postMessage(
      {
        type: "esphome-web-flash:firmware",
        nonce: "test-nonce-123",
        name: "bad-addr",
        parts: [{ address: -1, data: new ArrayBuffer(8) }],
      },
      "*",
    );
  });
  await new Promise((r) => setTimeout(r, 200));
  label = await popup.$eval("#status", (e) => e.textContent);
  if (!/malformed/i.test(label))
    fail("negative address not rejected: " + label);
  else console.log("PASS: negative address rejected at boundary");

  // 2d. a malformed origin= hash param must not wedge the ready handshake:
  // postMessage to a bad targetOrigin throws, and the receiver falls back to '*'.
  {
    const c = await browser.newPage();
    await c.goto(`${base}/opener.html`);
    const [pop2] = await Promise.all([
      new Promise((res) => c.once("popup", res)),
      c.evaluate((u) => window.__open(u), `${base}/#nonce=n2&origin=null`),
    ]);
    await pop2.waitForNetworkIdle({ idleTime: 300 }).catch(() => {});
    await new Promise((r) => setTimeout(r, 300));
    const ready2 = (await c.evaluate(() => window.__msgs)).find(
      (m) => m && m.type === "esphome-web-flash:ready",
    );
    if (!ready2) fail("ready not received when origin= hash param is malformed");
    else console.log("PASS: malformed origin falls back to '*', ready still sent");
    await pop2.close();
    await c.close();
  }

  // 3. correct nonce accepted -> button enabled, state mirrored back
  await a.evaluate(() => {
    window.__b.postMessage(
      {
        type: "esphome-web-flash:firmware",
        nonce: "test-nonce-123",
        name: "kitchen.factory.bin",
        erase: true,
        parts: [{ address: 0, data: new ArrayBuffer(2048) }],
      },
      "*",
    );
  });
  await new Promise((r) => setTimeout(r, 300));
  const enabled = await popup.$eval("#install", (b) => !b.disabled);
  label = await popup.$eval("#status", (e) => e.textContent);
  if (!enabled) fail("install button not enabled after firmware");
  else if (!/kitchen\.factory\.bin/.test(label))
    fail("status did not reflect firmware name: " + label);
  else console.log("PASS: firmware accepted, button enabled, status:", label);

  const stateMsg = (await a.evaluate(() => window.__msgs)).find(
    (m) => m && m.type === "esphome-web-flash:state",
  );
  if (!stateMsg) fail("no state message mirrored to opener");
  else console.log("PASS: state mirrored to opener ->", stateMsg.state);
} catch (e) {
  fail("exception: " + e.message);
} finally {
  await browser.close();
  server.close();
}

console.log(ok ? "\nALL PASS" : "\nFAILURES");
process.exit(ok ? 0 : 1);
