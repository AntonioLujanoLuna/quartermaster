// The failure this exists for is not a wrong number on a screen. It is a screen
// that throws.
//
// `renderApp` replaces the whole tree on every draw, and every draw runs from
// whatever the last read left in `state`. A read that has not landed leaves
// null, a read that failed leaves the previous value or null, and a campaign
// with nothing in it yet leaves empty. If any screen reaches into one of those
// without checking, the exception escapes `draw`, the tree is never replaced,
// and the table is looking at a dead page in the middle of an evening — with no
// control left to press, because the controls were what did not render.
//
// So the assertion is deliberately weak: it does not check what a screen says.
// It checks that every screen survives being asked to render before anything is
// there, which is the state every screen is in for the first moment of every
// launch.

import { describe, expect, it } from "vitest";
import { renderApp } from "../src/render.js";

const SCREENS = ["stash", "items", "party", "dossier", "loot", "treasury", "dice", "dm"];

/**
 * Handlers that record nothing and refuse nothing.
 *
 * Rendering never calls one — they are attached to events — so any property is
 * a function and none of them need to do anything. A Proxy rather than a list
 * keeps this test from failing the day a screen grows a handler, which is not
 * the failure it is looking for.
 */
const handlers = new Proxy({}, { get: () => () => {} });

/** Everything a fresh boot has: an actor, and nothing read yet. */
function emptyState(overrides = {}) {
  return {
    actor: { id: "1", isDm: false },
    participants: [],
    screen: "stash",
    home: null,
    stash: null,
    holdings: null,
    party: null,
    loot: null,
    treasury: null,
    combat: null,
    dice: { rolls: [], last: null },
    continuity: null,
    report: null,
    roster: [],
    dossier: null,
    playing: new Set(),
    live: "connecting",
    inputs: {},
    notice: null,
    prompt: null,
    busy: false,
    ...overrides,
  };
}

/** The same campaign after one evening: a session, a character, and some things. */
function populatedState(overrides = {}) {
  return emptyState({
    home: {
      character: { id: "c1", name: "Vex" },
      stash_count: 2,
      unclaimed: 1,
      active_session_number: 3,
      treasury: { cp: 0, sp: 0, ep: 0, gp: 12, pp: 0 },
    },
    stash: {
      items: [
        { id: "s1", item_name: "Rope", quantity: 3, provenance: "The dragon's hoard" },
        { id: "s2", item_name: "Torch", quantity: 1, provenance: null },
      ],
      total: 2,
    },
    holdings: {
      character: { id: "c1", name: "Vex" },
      items: [{ id: "h1", item_name: "Rope", quantity: 1, provenance: "The dragon's hoard" }],
      total_items: 1,
    },
    party: {
      characters: [
        {
          character_id: "c1",
          character_name: "Vex",
          lifecycle: "ACTIVE",
          items: [{ item_name: "Rope", quantity: 1, provenance: "The dragon's hoard" }],
        },
        {
          character_id: "c2",
          character_name: "Bram",
          lifecycle: "DEAD",
          items: [{ item_name: "Shield", quantity: 1, provenance: null }],
        },
      ],
      total_stacks: 2,
    },
    loot: {
      drops: [
        {
          drop_id: "d1",
          expires_at: "2026-08-25T20:00:00Z",
          items: [{ id: "di1", item_name: "Gem", remaining: 2 }],
        },
      ],
      total: 1,
    },
    treasury: {
      treasury: { cp: 0, sp: 0, ep: 0, gp: 12, pp: 0 },
      purse: { cp: 5, sp: 0, ep: 0, gp: 1, pp: 0 },
    },
    combat: { status: "OPEN", encounter: { channel_id: "9", opened_by: "1" } },
    continuity: {
      session_number: 2,
      where_ended: "At the gates",
      entries: [{ text: "Vex took a Rope." }],
      earlier: 4,
    },
    roster: [
      { id: "c1", name: "Vex", lifecycle: "ACTIVE", discord_user_id: "1" },
      { id: "c2", name: "Bram", lifecycle: "DEAD", discord_user_id: "2" },
    ],
    participants: [{ id: "1", username: "player" }],
    playing: new Set(["1"]),
    live: "live",
    ...overrides,
  });
}

describe("every screen renders before anything has been read", () => {
  for (const screen of SCREENS) {
    it(`${screen} survives an empty state as a player`, () => {
      const node = renderApp(emptyState({ screen }), handlers);
      expect(node).toBeInstanceOf(globalThis.HTMLElement);
    });

    it(`${screen} survives an empty state as a DM`, () => {
      const state = emptyState({ screen, actor: { id: "1", isDm: true } });
      expect(renderApp(state, handlers)).toBeInstanceOf(globalThis.HTMLElement);
    });

    it(`${screen} renders a campaign that has been played`, () => {
      const state = populatedState({ screen, actor: { id: "1", isDm: true } });
      expect(renderApp(state, handlers)).toBeInstanceOf(globalThis.HTMLElement);
    });
  }
});

describe("the states a screen reaches that are nobody's happy path", () => {
  it("renders a notice without a prompt and a prompt without a notice", () => {
    const notice = renderApp(
      populatedState({ notice: { tone: "bad", text: "That was refused." } }),
      handlers,
    );
    expect(notice.textContent).toContain("That was refused.");

    const prompt = renderApp(
      populatedState({
        prompt: { text: "Take 2 instead?", confirmLabel: "Take 2", fields: [], run: () => {} },
      }),
      handlers,
    );
    expect(prompt.textContent).toContain("Take 2 instead?");
  });

  it("does not offer a DM the controls a player is not shown", () => {
    const player = renderApp(populatedState({ screen: "stash" }), handlers);
    const dm = renderApp(
      populatedState({ screen: "stash", actor: { id: "1", isDm: true } }),
      handlers,
    );
    expect(player.textContent).not.toContain("Correct");
    expect(dm.textContent).toContain("Correct");
  });

  it("renders while an action is in flight, with every control disabled", () => {
    const node = renderApp(populatedState({ screen: "stash", busy: true }), handlers);
    const controls = [...node.querySelectorAll("button.press")];
    expect(controls.length).toBeGreaterThan(0);
    expect(controls.every((button) => button.disabled)).toBe(true);
  });
});

describe("the filter narrows a list without lying about the campaign", () => {
  it("shows only what matches, and says how many it is not showing", () => {
    const state = populatedState({ screen: "stash", inputs: { "filter:stash": "rope" } });
    const text = renderApp(state, handlers).textContent;
    expect(text).toContain("Rope");
    expect(text).not.toContain("Torch");
    expect(text).toContain("1 of 2 stacks shown");
  });

  it("matches where something came from, not only what it is called", () => {
    const state = populatedState({ screen: "stash", inputs: { "filter:stash": "dragon" } });
    const text = renderApp(state, handlers).textContent;
    expect(text).toContain("Rope");
    expect(text).not.toContain("Torch");
  });

  it("says the stash is empty and that nothing matched as two different answers", () => {
    const empty = renderApp(
      populatedState({ screen: "stash", stash: { items: [], total: 0 } }),
      handlers,
    );
    expect(empty.textContent).toContain("The Party Stash is empty.");

    const unmatched = renderApp(
      populatedState({ screen: "stash", inputs: { "filter:stash": "halberd" } }),
      handlers,
    );
    expect(unmatched.textContent).toContain("Nothing in the stash matches that.");
  });
});

describe("who has what", () => {
  it("names the holder beside the item", () => {
    const text = renderApp(populatedState({ screen: "party" }), handlers).textContent;
    expect(text).toContain("Rope");
    expect(text).toContain("Vex");
    expect(text).toContain("Shield");
  });

  it("marks a holder who has stopped playing, because that estate is outstanding", () => {
    const text = renderApp(populatedState({ screen: "party" }), handlers).textContent;
    expect(text).toContain("dead");
  });

  it("filters by holder as well as by item", () => {
    const state = populatedState({ screen: "party", inputs: { "filter:party": "bram" } });
    const text = renderApp(state, handlers).textContent;
    expect(text).toContain("Shield");
    expect(text).not.toContain("Rope");
  });

  it("presses nothing, because moving somebody else's property is not a thing", () => {
    const node = renderApp(populatedState({ screen: "party" }), handlers);
    // The tabs and the roster carry buttons; the listing itself must not.
    expect(node.querySelectorAll(".screen .listing button").length).toBe(0);
  });
});

describe("my items", () => {
  it("says where a held stack came from, as the Party Stash always has", () => {
    const text = renderApp(populatedState({ screen: "items" }), handlers).textContent;
    expect(text).toContain("The dragon's hoard");
  });

  it("tells a player with no character why there is nothing to carry", () => {
    const state = populatedState({
      screen: "items",
      holdings: { character: null, items: [], total_items: 0 },
    });
    expect(renderApp(state, handlers).textContent).toContain("no active character");
  });
});
