// The screens page's dev server: `pnpm exec vite --config src/session/screens/vite.config.ts`.
// It draws the session view over recorded frames (tests/fixtures/session-view) so each state can be
// looked at and photographed in a real browser. It is not part of the build: the hub never serves it.
import { readFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import { defineConfig, type Plugin } from "vite";

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, "../../../../..");

function tokens(): Plugin {
  return {
    name: "swe-screens-tokens",
    configureServer(server) {
      server.middlewares.use("/tokens.css", (_req, res) => {
        res.setHeader("Content-Type", "text/css; charset=utf-8");
        res.end(readFileSync(join(repo, "src/swe/webapp/static/tokens.css")));
      });
    },
  };
}

export default defineConfig({
  root: here,
  publicDir: false,
  plugins: [tailwindcss(), tokens()],
  server: { port: 5199, strictPort: true, fs: { allow: [repo] } },
});
