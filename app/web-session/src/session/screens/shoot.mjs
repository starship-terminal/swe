// Takes the session view's screens with Chrome, over the DevTools protocol, with no dependency but
// Node 22 (its built-in WebSocket) and a Chrome binary:
//
//   pnpm exec vite --config src/session/screens/vite.config.ts &      # the screens page, port 5199
//   node src/session/screens/shoot.mjs <out-dir> [scene ...]
//
// Each scene is drawn at 1280x800 and at a 390x844 phone (touch, mobile viewport), in light and in
// dark, and saved as <out-dir>/<scene>-<desktop|phone>-<light|dark>.png. `CHROME` names the binary
// (default: Google Chrome on macOS). A scene is a name from SCENES below: a query for the page.
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const SCENES = {
  default: "scene=default",
  "work-group-expanded": "scene=default&click=Worked%20for",
  "tool-expanded": "scene=default&click=Worked%20for|Edit%20src%2Fapp.py",
  streaming: "scene=streaming",
  "stopped-tool": "scene=stopped-tool&click=Worked%20%C2%B7",
  empty: "scene=empty",
  loading: "scene=loading",
  offline: "scene=offline",
  reconnecting: "scene=reconnecting",
  "session-gone": "scene=gone",
  "hub-not-answering": "scene=hub-down",
  "outbox-states": "scene=sends",
  "outbox-storage-down": "scene=sends-storage-down",
  "outbox-loading": "scene=sends-loading",
  "composer-empty": "scene=composer-empty",
  "composer-typing": "scene=composer-typing",
  "composer-multiline": "scene=composer-multiline",
  "composer-sending": "scene=composer-sending",
  "composer-busy": "scene=composer-busy",
  "composer-queued": "scene=composer-queued",
  "composer-queued-edit": "scene=composer-queued-edit",
  "composer-queued-many": "scene=composer-queued-many",
  "composer-approval-confirm": "scene=composer-approval-confirm",
  "composer-approval": "scene=composer-approval",
  "composer-approval-offline": "scene=composer-approval-offline",
  "composer-input-text": "scene=composer-input-text",
  "composer-input-choice": "scene=composer-input-choice",
  "composer-offline": "scene=composer-offline",
  "composer-storage-down": "scene=composer-storage-down",
  "composer-loading": "scene=composer-loading",
  "composer-no-steer": "scene=composer-no-steer",
  "composer-read-only": "scene=composer-read-only",
  "composer-attachments": "scene=composer-attachments",
  "composer-hostile": "scene=composer-hostile",
};
const SIZES = { desktop: { width: 1280, height: 800, mobile: false }, phone: { width: 390, height: 844, mobile: true } };
const BASE = process.env.SCREENS_URL ?? "http://localhost:5199/";
const CHROME = process.env.CHROME ?? "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const PORT = 9333;

const out = process.argv[2];
if (!out) throw new Error("usage: node shoot.mjs <out-dir> [scene ...]");
mkdirSync(out, { recursive: true });
const wanted = process.argv.slice(3);
const names = wanted.length > 0 ? wanted : Object.keys(SCENES);

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const profile = mkdtempSync(join(tmpdir(), "swe-screens-"));
const chrome = spawn(CHROME, [
  "--headless=new",
  `--remote-debugging-port=${PORT}`,
  `--user-data-dir=${profile}`,
  "--no-first-run",
  "--disable-gpu",
  "--hide-scrollbars",
  "about:blank",
], { stdio: "ignore" });

async function target() {
  for (let i = 0; i < 100; i++) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((entry) => entry.type === "page");
      if (page) return page.webSocketDebuggerUrl;
    } catch {
      // Chrome is still starting.
    }
    await sleep(100);
  }
  throw new Error("Chrome did not start");
}

try {
  const socket = new WebSocket(await target());
  await new Promise((resolve, reject) => {
    socket.onopen = resolve;
    socket.onerror = reject;
  });
  let id = 0;
  const waiting = new Map();
  socket.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.id !== undefined) {
      waiting.get(message.id)?.(message);
      waiting.delete(message.id);
    }
  };
  const call = (method, params = {}) =>
    new Promise((resolve, reject) => {
      const mine = ++id;
      waiting.set(mine, (message) => (message.error ? reject(new Error(`${method}: ${message.error.message}`)) : resolve(message.result)));
      socket.send(JSON.stringify({ id: mine, method, params }));
    });

  await call("Page.enable");
  await call("Runtime.enable");
  for (const name of names) {
    const query = SCENES[name];
    if (query === undefined) throw new Error(`no scene ${name}`);
    for (const [sizeName, size] of Object.entries(SIZES)) {
      for (const theme of ["light", "dark"]) {
        await call("Emulation.setDeviceMetricsOverride", { width: size.width, height: size.height, deviceScaleFactor: 1, mobile: size.mobile });
        await call("Emulation.setTouchEmulationEnabled", { enabled: size.mobile });
        await call("Page.navigate", { url: `${BASE}?${query}&theme=${theme}` });
        for (let i = 0; i < 100; i++) {
          const { result } = await call("Runtime.evaluate", { expression: "window.__ready === true", returnByValue: true });
          if (result.value === true) break;
          await sleep(100);
        }
        await sleep(400); // the last animation settles
        const shot = await call("Page.captureScreenshot", { format: "png" });
        const file = join(out, `${name}-${sizeName}-${theme}.png`);
        writeFileSync(file, Buffer.from(shot.data, "base64"));
        console.log(file);
      }
    }
  }
  socket.close();
} finally {
  chrome.kill();
}
