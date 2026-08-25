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

describe("a screen somebody cannot see, and a screen too narrow for a table", () => {
  it("says which screen is current, as a tablist rather than as a row of buttons", () => {
    const node = renderApp(populatedState({ screen: "loot" }), handlers);
    const tabs = [...node.querySelectorAll('[role="tab"]')];
    expect(tabs.length).toBeGreaterThan(1);
    const current = tabs.filter((tab) => tab.getAttribute("aria-selected") === "true");
    expect(current).toHaveLength(1);
    expect(current[0].textContent).toContain("Loot");
    // One tab stop for the whole strip; the arrows move within it.
    expect(tabs.filter((tab) => tab.tabIndex === 0)).toHaveLength(1);
  });

  it("points the panel at the tab that named it", () => {
    const node = renderApp(populatedState({ screen: "treasury" }), handlers);
    const panel = node.querySelector('[role="tabpanel"]');
    expect(panel.getAttribute("aria-labelledby")).toBe("tab-treasury");
    expect(node.querySelector("#tab-treasury")).not.toBeNull();
  });

  it("announces a refusal over what is being read and a receipt after it", () => {
    const bad = renderApp(
      populatedState({ notice: { tone: "bad", text: "That was refused." } }),
      handlers,
    );
    expect(bad.querySelector(".notice").getAttribute("aria-live")).toBe("assertive");

    const ok = renderApp(
      populatedState({ notice: { tone: "ok", text: "Took 1 Rope." } }),
      handlers,
    );
    expect(ok.querySelector(".notice").getAttribute("aria-live")).toBe("polite");
  });

  it("asks its one irreversible question as a dialog, named by the question", () => {
    const node = renderApp(
      populatedState({
        prompt: { text: "Use 1 Rope?", confirmLabel: "Use it", fields: [], run: () => {} },
      }),
      handlers,
    );
    const box = node.querySelector('[role="dialog"]');
    expect(box.getAttribute("aria-modal")).toBe("true");
    expect(node.querySelector(`#${box.getAttribute("aria-labelledby")}`).textContent).toContain(
      "Use 1 Rope?",
    );
  });

  it("gives every named cell the heading it sits under, for the phone layout", () => {
    const node = renderApp(populatedState({ screen: "stash" }), handlers);
    const row = node.querySelector(".listing tbody tr");
    const labels = [...row.children].map((cell) => cell.dataset.label);
    expect(labels[0]).toBe("Item");
    expect(labels[2]).toBe("Where it came from");
    // The controls column is deliberately headed with nothing, and a card that
    // printed a blank label in front of two buttons would be printing the
    // absence of a heading rather than a heading.
    expect(row.querySelector("td.controls").dataset.label).toBeUndefined();
  });

  it("does not put a column name in front of a sentence about the whole list", () => {
    const node = renderApp(
      populatedState({ screen: "stash", stash: { items: [], total: 0 } }),
      handlers,
    );
    const cell = node.querySelector(".listing tbody td[colspan]");
    expect(cell.dataset.label).toBeUndefined();
  });
});

describe("a dossier has somewhere to come from", () => {
  const dmWithRoster = (overrides = {}) =>
    populatedState({
      screen: "dossier",
      actor: { id: "1", isDm: true },
      dossier: {
        status: "UNAVAILABLE",
        reason: "No verified snapshot is available.",
        character: null,
      },
      ...overrides,
    });

  it("offers the DM an import form on the screen that reads it back", () => {
    const node = renderApp(dmWithRoster(), handlers);
    expect(node.textContent).toContain("Import a sheet snapshot");
    expect(node.querySelector('[data-input-key="dossier:scores"]')).not.toBeNull();
    expect(node.querySelector('[data-input-key="dossier:character"]')).not.toBeNull();
  });

  it("offers it beside a snapshot that already exists, because sheets change", () => {
    const node = renderApp(
      dmWithRoster({
        dossier: {
          status: "CURRENT",
          character: { id: "c1", name: "Vex" },
          snapshot: {
            system: "D&D 5e",
            rules_version: "2014",
            source: "MANUAL_IMPORT",
            observed_at: "2026-08-25T10:00:00Z",
            level: 4,
            ability_modifiers: { STR: 3 },
            ability_scores: { STR: 16 },
            saving_throws: {},
            spell_resources: {},
            equipped: {},
          },
        },
      }),
      handlers,
    );
    expect(node.textContent).toContain("Vex dossier");
    expect(node.textContent).toContain("Import a sheet snapshot");
  });

  it("shows a player neither the form nor a reason to want one", () => {
    const node = renderApp(dmWithRoster({ actor: { id: "1", isDm: false } }), handlers);
    expect(node.textContent).not.toContain("Import a sheet snapshot");
  });

  it("says so rather than offering a form with nobody to import for", () => {
    const node = renderApp(dmWithRoster({ roster: [] }), handlers);
    expect(node.textContent).toContain("No active character is registered");
  });

  it("keeps the recording beside where the table stopped", () => {
    const node = renderApp(
      populatedState({
        home: { ...populatedState().home, active_session_number: null },
        continuity: {
          active_session_number: null,
          previous: {
            session_number: 2,
            where_ended: "At the gates",
            recording_url: "https://rec/2",
          },
          recap: [],
          recap_total: 0,
        },
      }),
      handlers,
    );
    const link = node.querySelector("a.recording");
    expect(link.href).toBe("https://rec/2");
    expect(link.rel).toContain("noopener");
  });
});
