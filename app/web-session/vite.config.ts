// The session view's build. Output goes straight into the Python package,
// src/swe/webapp/static/session/, and is committed: the hub serves it at
// /session/ with no Node at runtime. Names are fixed so the hub can cache
// them: index.html, session-view.js and session-view.css (stable, served
// no-cache) and assets/*-<hash>.* (immutable). third-party-licenses.md
// carries the notices of the packages bundled in. build-info.json records the
// source hash tests/test_webapp_session_view.py checks for freshness.
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";

import { sourceFiles, sourceHash } from "./scripts/source-hash.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const staticDir = resolve(here, "../../src/swe/webapp/static");
const outDir = join(staticDir, "session");
const publicDir = join(here, "public");

/** Emit build-info.json: the source hash and the list of files built. */
function buildInfo(): Plugin {
  return {
    name: "swe-build-info",
    apply: "build",
    generateBundle: {
      order: "post",
      handler(_options, bundle) {
        const sources = sourceFiles(here);
        const publicFiles = readdirSync(publicDir).filter((name) => !name.startsWith("."));
        const outputs = [...Object.keys(bundle), ...publicFiles, "build-info.json"].sort();
        const info = {
          hash_rule: "app/web-session/scripts/source-hash.mjs",
          source_hash: sourceHash(sources),
          sources,
          outputs,
        };
        this.emitFile({
          type: "asset",
          fileName: "build-info.json",
          source: `${JSON.stringify(info, null, 2)}\n`,
        });
      },
    },
  };
}

/** `pnpm dev` only: serve the shared tokens.css as the hub does, at /tokens.css. */
function devTokens(): Plugin {
  return {
    name: "swe-dev-tokens",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/tokens.css", (_req, res) => {
        res.setHeader("Content-Type", "text/css; charset=utf-8");
        res.end(readFileSync(join(staticDir, "tokens.css")));
      });
    },
  };
}

export default defineConfig({
  root: here,
  base: "/session/",
  publicDir,
  plugins: [tailwindcss(), buildInfo(), devTokens()],
  server: {
    // The hub's API, for the milestones that start reading it.
    proxy: { "/v1": process.env.SWE_HUB_URL ?? "http://127.0.0.1:8477" },
  },
  build: {
    outDir,
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
    // The licence texts of every package bundled in (React's MIT notice):
    // the minifier drops their banners, so they ship beside the chunks.
    license: { fileName: "third-party-licenses.md" },
    reportCompressedSize: true,
    modulePreload: { polyfill: false },
    rolldownOptions: {
      input: {
        index: join(here, "index.html"),
        "session-view": join(here, "src/main.tsx"),
      },
      output: {
        entryFileNames: (chunk) =>
          chunk.name === "session-view" ? "session-view.js" : "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: (asset) =>
          asset.names.includes("session-view.css") ? "session-view.css" : "assets/[name]-[hash][extname]",
        codeSplitting: {
          groups: [{ name: "react", test: /[\\/]node_modules[\\/](?:react|react-dom|scheduler)[\\/]/ }],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
