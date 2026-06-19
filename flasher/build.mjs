import * as esbuild from "esbuild";
import { cpSync, mkdirSync } from "node:fs";

const outdir = "dist";
const serve = process.argv.includes("--serve");

mkdirSync(outdir, { recursive: true });

/** @type {import('esbuild').BuildOptions} */
const options = {
  entryPoints: ["src/main.ts"],
  bundle: true,
  format: "esm",
  outfile: `${outdir}/flasher.js`,
  sourcemap: true,
  target: ["es2020"],
  minify: !serve,
  logLevel: "info",
};

cpSync("index.html", `${outdir}/index.html`);

if (serve) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
  const { hosts, port } = await ctx.serve({ servedir: outdir });
  console.log(`Serving flasher on http://${hosts[0]}:${port}`);
} else {
  await esbuild.build(options);
  console.log("Built flasher -> dist/");
}
