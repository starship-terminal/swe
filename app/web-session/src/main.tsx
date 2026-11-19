// The session view bundle (session-view.js, a stable name). It exposes
// SWESessionView.mount(el, {key}) for any page that loads it: the
// standalone /session/ page (boot.ts) today, the web app's chat in G14.
import "./index.css";

import { StrictMode } from "react";
import { createRoot, type Root } from "react-dom/client";

import { SessionShell } from "./SessionShell";

export interface MountOptions {
  /** The hub session key, `tool:sid`. */
  key: string;
}

export interface MountedView {
  unmount(): void;
}

export interface SessionViewApi {
  mount(el: Element, options: MountOptions): MountedView;
}

const roots = new WeakMap<Element, Root>();

/** Render the session view into `el`, replacing any view already there. */
export function mount(el: Element, options: MountOptions): MountedView {
  roots.get(el)?.unmount();
  const root = createRoot(el);
  roots.set(el, root);
  root.render(
    <StrictMode>
      <SessionShell sessionKey={options.key} />
    </StrictMode>,
  );
  return {
    unmount() {
      if (roots.get(el) === root) roots.delete(el);
      root.unmount();
    },
  };
}

declare global {
  interface Window {
    SWESessionView?: SessionViewApi;
  }
}

window.SWESessionView = { mount };
