import * as esbuild from "esbuild";
import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";

const outdir = "dist";
const vendor = "vendor";
const serve = process.argv.includes("--serve");

// The decoder release this page is built against. Pinned by tag AND digest: a
// retagged or tampered asset fails the build instead of silently shipping new
// bytes to every dashboard. Bumping is deliberate; update both constants
// together (`shasum -a 256` on the downloaded asset prints the second).
const DECODER_TAG = "v2.0.1";
const DECODER_SHA256 = "a67804215b82e9632b8210c2f625e3b35b2dd17540e05c99455d3f060bae8e9e";
const ASSET = "esp_stacktrace_decoder_wasm.tar.gz";
const DECODER_URL = `https://github.com/esphome/esp-stacktrace-decoder/releases/download/${DECODER_TAG}/${ASSET}`;

// wasm-bindgen --target web output. The glue resolves the wasm with
// `new URL('..._bg.wasm', import.meta.url)`, which is why it stays unbundled
// (see the esbuild `external` below).
const GLUE = "esp_stacktrace_decoder_rs.js";
const WASM = "esp_stacktrace_decoder_rs_bg.wasm";
// Records what vendor/ holds, so a tag or digest bump re-downloads instead of
// building against whatever a previous run left behind.
const STAMP = `${vendor}/.stamp`;
const stampValue = `${DECODER_TAG} ${DECODER_SHA256}`;

async function vendorDecoder() {
  if (existsSync(STAMP) && readFileSync(STAMP, "utf8") === stampValue) return;
  rmSync(vendor, { recursive: true, force: true });
  mkdirSync(vendor, { recursive: true });
  console.log(`Downloading esp-stacktrace-decoder ${DECODER_TAG} ...`);
  const res = await fetch(DECODER_URL);
  if (!res.ok) throw new Error(`Download failed: ${res.status} ${DECODER_URL}`);
  const bytes = Buffer.from(await res.arrayBuffer());
  const digest = createHash("sha256").update(bytes).digest("hex");
  if (digest !== DECODER_SHA256) {
    throw new Error(
      `Digest mismatch for ${ASSET}\n  expected ${DECODER_SHA256}\n  got      ${digest}\n` +
        "Refusing to build. If the bump is intentional, update DECODER_SHA256.",
    );
  }
  const tarball = `${vendor}/${ASSET}`;
  writeFileSync(tarball, bytes);
  execFileSync("tar", ["xzf", ASSET], { cwd: vendor, stdio: "inherit" });
  for (const file of [GLUE, WASM]) {
    if (!existsSync(`${vendor}/${file}`)) throw new Error(`${ASSET} is missing ${file}`);
  }
  writeFileSync(STAMP, stampValue);
}

await vendorDecoder();
mkdirSync(outdir, { recursive: true });

/** @type {import('esbuild').BuildOptions} */
const options = {
  entryPoints: ["src/main.ts"],
  bundle: true,
  format: "esm",
  outfile: `${outdir}/decoder.js`,
  sourcemap: true,
  target: ["es2020"],
  minify: !serve,
  logLevel: "info",
  // Left unbundled on purpose: the glue locates the wasm relative to its own
  // module URL, so inlining it here would make that lookup resolve against
  // decoder.js and 404. The browser loads it from dist/ beside the wasm.
  external: [`./${GLUE}`],
};

cpSync("index.html", `${outdir}/index.html`);
cpSync(`${vendor}/${GLUE}`, `${outdir}/${GLUE}`);
cpSync(`${vendor}/${WASM}`, `${outdir}/${WASM}`);

if (serve) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  const { hosts, port } = await ctx.serve({ servedir: outdir });
  console.log(`Serving decoder on http://${hosts[0]}:${port}`);
} else {
  await esbuild.build(options);
  console.log("Built decoder -> dist/");
}
