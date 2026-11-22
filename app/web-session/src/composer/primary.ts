// The primary button's press machine: when a click sends, and when it is the tail of a hold that opened the menu.
//
// A hold (a mouse or pen held down, a finger held) opens the menu behind the button. The release that ends it
// then brings a click, and browsers deliver the events around that click in different orders:
//
//   mouse   pointerdown, (hold: menu opens), pointerup, click
//   touch   pointerdown, (hold: menu opens), pointerup, pointerout, pointerleave, click     (Chrome, recorded)
//   pen     as touch, or as mouse
//   either, released off the button:  pointerdown, (menu), pointerleave, (pointerup elsewhere), no click
//
// So nothing here depends on the order. The rule is: once a hold has opened the menu, a click that comes from a
// pointer (`detail` > 0) and not from a new press is the hold's tail and sends nothing; a new press (a pointerdown, a
// key) ends that. A click with no pointer (`detail` 0: a key's, an assistive technology's bare activation) is
// always a press of its own. The table is in docs/specs/G13-composer.md; `primary.test.ts` has a row per cell.

export type PrimaryState =
  /** Nothing is held. */
  | "idle"
  /** A pointer is down on the button; the hold timer runs; the menu is closed. */
  | "pressed"
  /** A hold opened the menu and the pointer is still down. */
  | "held"
  /** A hold opened the menu and the pointer is up (or gone): its click, if one comes, is not a press. */
  | "released";

export type PrimaryEvent =
  /** The main button (or a touch, or a pen) went down on the button. `holdable`: a hold can open the menu now. */
  | { type: "down"; holdable: boolean }
  | { type: "up" }
  /** The pointer left the button. */
  | { type: "leave" }
  | { type: "cancel" }
  /** The hold timer ran out. */
  | { type: "hold" }
  /** A click, with the event's `detail` (0 for a key or an assistive technology, 1 or more for a pointer). */
  | { type: "click"; detail: number }
  /** A right click (or a control click) asks for the menu; `menuOffered` says whether there is one. */
  | { type: "contextmenu"; menuOffered: boolean }
  /** A key went down on the button. */
  | { type: "key" }
  | { type: "blur" };

export type PrimaryAction =
  | "none"
  /** Start the hold timer. */
  | "start-hold"
  /** Stop the hold timer. */
  | "stop-hold"
  | "open-menu"
  /** This click is a press: send. */
  | "send"
  /** This click is the tail of a hold: do nothing. */
  | "swallow";

export interface Step {
  state: PrimaryState;
  action: PrimaryAction;
}

const at = (state: PrimaryState, action: PrimaryAction = "none"): Step => ({ state, action });

/** One step of the machine. Pure: the caller keeps the state. */
export function stepPrimary(state: PrimaryState, event: PrimaryEvent): Step {
  switch (event.type) {
    case "down":
      // A new press ends whatever was left of an earlier hold.
      return event.holdable ? at("pressed", "start-hold") : at("pressed");
    case "hold":
      return state === "pressed" ? at("held", "open-menu") : at(state);
    case "up":
      if (state === "pressed") return at("idle", "stop-hold"); // a plain press: its click follows and sends
      if (state === "held") return at("released");
      return at(state);
    case "leave":
    case "cancel":
      // Before the menu opened the hold is off (no click will come). After it, the click may still come (touch
      // sends leave before it) or not (the pointer was let go elsewhere): the state stays until a new press.
      if (state === "pressed") return at("idle", "stop-hold");
      if (state === "held") return at("released");
      return at(state);
    case "click":
      // A pointer's click that is the hold's tail. Spent by the first one; a new press cannot be mistaken for it, since
      // it begins with a pointerdown, which has already ended `released`.
      if (event.detail > 0 && (state === "released" || state === "held")) return at("idle", "swallow");
      return at("idle", "send");
    case "contextmenu":
      if (!event.menuOffered) return at(state);
      // Opened with a pointer down (a control click): its release is the menu's. With none (a right click): nothing to swallow.
      return state === "pressed" ? at("held", "open-menu") : at(state === "idle" ? "idle" : state, "open-menu");
    case "key":
      // A key is a new press (or an arrow that opens the menu, which the caller handles): it ends a hold's tail.
      return at(state === "released" ? "idle" : state);
    case "blur":
      // The menu a hold opened takes the focus off the button: that is no end to the hold.
      return state === "pressed" ? at("idle", "stop-hold") : at(state);
  }
}
