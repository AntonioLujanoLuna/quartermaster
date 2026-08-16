// Rendering, kept apart from the handshake so the screen can grow without the
// boot sequence growing with it.
//
// Everything is built with createElement rather than innerHTML. Item names and
// provenance are typed by people at the table, and a stash that renders them as
// markup is a stash that renders whatever someone types.
//
// Nothing here decides anything. It reads state, and it calls a handler when
// somebody presses something — which is what lets the action layer own the
// idempotency key, the confirmations, and the refusals without knowing what a
// table row is.

import { formatCoin, hasCoin } from "./format.js";

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function button(label, { onPress, style, busy, title }) {
  const node = element("button", style ? `press ${style}` : "press", label);
  node.type = "button";
  // Every control is disabled while an action is in flight. A second press
  // during the round trip would be a second action with a second key, which is
  // exactly the double-take the receipt cannot protect anyone from.
  node.disabled = Boolean(busy);
  if (title) node.title = title;
  node.addEventListener("click", onPress);
  return node;
}

/** A number field whose value survives the redraw a live change causes. */
function quantityField(state, key, { max, handlers }) {
  const node = element("input", "quantity-field");
  node.type = "number";
  node.min = "1";
  if (max !== undefined) node.max = String(max);
  node.value = state.inputs[key] ?? "1";
  node.dataset.inputKey = key;
  node.addEventListener("input", () => handlers.setInput(key, node.value));
  return node;
}

function textField(state, key, placeholder, handlers) {
  const node = element("input", "text-field");
  node.type = "text";
  node.placeholder = placeholder;
  node.value = state.inputs[key] ?? "";
  node.dataset.inputKey = key;
  node.addEventListener("input", () => handlers.setInput(key, node.value));
  return node;
}

function amountIn(state, key, fallback = 1) {
  const typed = Number.parseInt(state.inputs[key] ?? "", 10);
  return Number.isFinite(typed) && typed > 0 ? typed : fallback;
}

export function renderStatus(message) {
  return element("p", "status", message);
}

export function renderError(message) {
  const box = element("div", "error");
  box.append(element("h2", null, "Quartermaster could not open"), element("p", null, message));
  return box;
}

// Chrome ---------------------------------------------------------------------

// What the screen is allowed to claim about itself. A surface that reads live
// has to say when it has stopped, or the table trusts a number that stopped
// moving ten minutes ago — which is the failure the panels had by design.
const LIVE_LABELS = {
  live: "Live",
  connecting: "Connecting…",
  offline: "Reconnecting…",
};

function renderLive(status) {
  const state = LIVE_LABELS[status] ? status : "connecting";
  const badge = element("span", `live live-${state}`);
  badge.append(element("span", "live-dot"), element("span", null, LIVE_LABELS[state]));
  return badge;
}

function renderSummary(home) {
  const bar = element("div", "summary");
  if (!home) return bar;
  const session =
    home.active_session_number !== null
      ? `Session ${home.active_session_number} · in progress`
      : "No session in progress";
  bar.append(element("span", "session", session));
  bar.append(element("span", "coin", formatCoin(home.treasury)));
  if (home.character) {
    bar.append(element("span", "character", `Playing ${home.character.name}`));
  } else {
    bar.append(element("span", "character muted", "No active character"));
  }
  if (hasCoin(home.purse)) {
    bar.append(element("span", "purse", `Your coin: ${formatCoin(home.purse)}`));
  }
  return bar;
}

const SCREENS = [
  { id: "stash", label: "Party Stash" },
  { id: "items", label: "My Items" },
  { id: "loot", label: "Loot" },
  { id: "treasury", label: "Treasury" },
];

function renderTabs(state, handlers) {
  const nav = element("nav", "tabs");
  for (const screen of SCREENS) {
    const tab = element("button", state.screen === screen.id ? "tab tab-current" : "tab", screen.label);
    tab.type = "button";
    if (screen.id === "loot" && state.home?.unclaimed) {
      tab.append(element("span", "badge", state.home.unclaimed));
    }
    tab.addEventListener("click", () => handlers.select(screen.id));
    nav.append(tab);
  }
  return nav;
}

/**
 * What just happened, said once.
 *
 * The panels answered a press with a separate ephemeral message, so that
 * reporting a committed mutation never depended on a re-render succeeding.
 * The same rule holds here for the same reason: this line is written from the
 * mutation's own answer and is not touched by the reads that follow it.
 */
function renderNotice(state, handlers) {
  if (!state.notice) return null;
  const box = element("div", `notice notice-${state.notice.tone}`);
  box.append(element("p", null, state.notice.text));
  box.append(button("Dismiss", { style: "quiet", onPress: handlers.dismissNotice }));
  return box;
}

/**
 * A question the player has to answer before anything happens.
 *
 * Two things arrive here. A quantity that moved between deciding and pressing,
 * which is the confirmation the handles have always produced. And the one
 * action with no way back — using something up — which asks before it spends
 * rather than reporting after.
 */
function renderPrompt(state, handlers) {
  if (!state.prompt) return null;
  const box = element("div", "prompt");
  box.append(element("p", "prompt-text", state.prompt.text));
  const controls = element("div", "prompt-controls");
  for (const field of state.prompt.fields || []) {
    controls.append(textField(state, `prompt:${field.name}`, field.placeholder, handlers));
  }
  controls.append(
    button(state.prompt.confirmLabel || "Confirm", {
      style: "primary",
      busy: state.busy,
      onPress: handlers.confirmPrompt,
    }),
  );
  controls.append(button("Cancel", { style: "quiet", onPress: handlers.dismissPrompt }));
  box.append(controls);
  return box;
}

function renderRoster(state, handlers) {
  const aside = element("aside", "roster");
  aside.append(element("h2", null, `At the table (${state.participants.length})`));
  const list = element("ul");
  for (const person of state.participants) {
    const name = person.nickname || person.global_name || person.username || "Someone";
    const row = element("li");
    row.append(element("span", "roster-name", name));
    // Registering a character is a DM control on the panel, and the roster is
    // where the Activity already knows who is actually here — which is the
    // user select, without a user select.
    if (state.actor?.isDm && !state.playing.has(String(person.id))) {
      row.append(
        button("Register…", {
          style: "quiet",
          busy: state.busy,
          onPress: () => handlers.register(person.id, name),
        }),
      );
    }
    list.append(row);
  }
  if (state.participants.length === 0) {
    list.append(element("li", "muted", "Nobody else yet"));
  }
  aside.append(list);
  return aside;
}

function emptyRow(columns, message) {
  const row = element("tr");
  const cell = element("td", "muted", message);
  cell.colSpan = columns;
  row.append(cell);
  return row;
}

function table(headings) {
  const node = element("table", "listing");
  const head = element("thead");
  const headRow = element("tr");
  for (const label of headings) headRow.append(element("th", null, label));
  head.append(headRow);
  node.append(head);
  const body = element("tbody");
  node.append(body);
  return [node, body];
}

// Screens --------------------------------------------------------------------

function renderStashScreen(state, handlers) {
  const screen = element("section", "screen");
  const stash = state.stash || { items: [], total: 0 };
  const [listing, body] = table(["Item", "Qty", "Where it came from", ""]);
  // No truncation and no "dropped N entries". The list scrolls, which is the
  // whole reason this surface exists.
  for (const item of stash.items) {
    const row = element("tr");
    row.append(element("td", "name", item.item_name));
    row.append(element("td", "quantity", item.quantity));
    row.append(element("td", "provenance muted", item.provenance || "—"));
    const controls = element("td", "controls");
    controls.append(
      button("Take 1", { busy: state.busy, onPress: () => handlers.take(item, 1) }),
    );
    if (item.quantity > 1) {
      controls.append(
        button("Take all", {
          style: "primary",
          busy: state.busy,
          onPress: () => handlers.take(item, "all"),
        }),
      );
    }
    row.append(controls);
    body.append(row);
  }
  if (stash.items.length === 0) body.append(emptyRow(4, "The Party Stash is empty."));
  screen.append(listing);
  screen.append(element("p", "count muted", `${stash.total} stacks`));
  if (!state.home?.character) {
    screen.append(
      element(
        "p",
        "count muted",
        "Taking something needs a registered character. Ask the DM to register one.",
      ),
    );
  }
  return screen;
}

/**
 * Where a give is headed. One select for the screen rather than one per row:
 * a player hands things to one person at a time, and repeating the roster on
 * every line is a lot of screen for a choice that rarely changes.
 */
function renderDestination(state, handlers, key) {
  const wrap = element("label", "destination");
  wrap.append(element("span", "muted", "Give to"));
  const select = element("select", "select");
  select.dataset.inputKey = key;
  const options = [{ value: "party", label: "The Party Stash" }];
  for (const character of state.roster || []) {
    if (character.lifecycle !== "ACTIVE") continue;
    if (state.home?.character && character.id === state.home.character.id) continue;
    options.push({ value: character.id, label: character.name });
  }
  for (const option of options) {
    const node = element("option", null, option.label);
    node.value = option.value;
    node.selected = (state.inputs[key] ?? "party") === option.value;
    select.append(node);
  }
  select.addEventListener("change", () => handlers.setInput(key, select.value));
  wrap.append(select);
  return wrap;
}

function renderItemsScreen(state, handlers) {
  const screen = element("section", "screen");
  const holdings = state.holdings || { character: null, items: [], total_items: 0 };
  if (!holdings.character) {
    screen.append(
      element("p", "muted", "You have no active character, so there is nothing to carry yet."),
      element("p", "muted", "A DM registers one — from the roster beside this list."),
    );
    return screen;
  }
  screen.append(renderDestination(state, handlers, "destination"));

  const [listing, body] = table(["Item", "Held", "How many", ""]);
  for (const item of holdings.items) {
    const key = `give:${item.id}`;
    const row = element("tr");
    row.append(element("td", "name", item.item_name));
    row.append(element("td", "quantity", item.quantity));
    const field = element("td", "field");
    field.append(quantityField(state, key, { max: item.quantity, handlers }));
    row.append(field);
    const controls = element("td", "controls");
    // The destination is read when the button is pressed rather than when the
    // row is built: changing the select does not redraw the screen, so a value
    // captured here would be whatever it was before the player chose.
    const destination = () => state.inputs.destination ?? "party";
    controls.append(
      button("Give", {
        busy: state.busy,
        onPress: () => handlers.giveQuantity(item, destination(), amountIn(state, key)),
      }),
    );
    if (item.quantity > 1) {
      controls.append(
        button("Give all", {
          style: "primary",
          busy: state.busy,
          onPress: () => handlers.give(item, destination(), "all"),
        }),
      );
    }
    controls.append(
      button("Use…", {
        style: "danger",
        busy: state.busy,
        title: "Spends it. It leaves the campaign and the session log says so.",
        onPress: () => handlers.use(item, amountIn(state, key)),
      }),
    );
    row.append(controls);
    body.append(row);
  }
  if (holdings.items.length === 0) {
    body.append(emptyRow(4, `${holdings.character.name} is carrying nothing.`));
  }
  screen.append(listing);
  screen.append(
    element("p", "count muted", `${holdings.character.name} · ${holdings.total_items} stacks`),
  );
  return screen;
}

function renderLootScreen(state, handlers) {
  const screen = element("section", "screen");
  const drops = state.loot?.drops || [];
  if (drops.length === 0) {
    screen.append(element("p", "muted", "No Loot Drop is open."));
    return screen;
  }
  for (const drop of drops) {
    const block = element("div", "drop");
    block.append(element("h2", null, `Loot Drop · closes ${drop.expires_at}`));
    const [listing, body] = table(["Item", "Left", "How many", ""]);
    for (const item of drop.items) {
      const key = `claim:${item.id}`;
      const row = element("tr");
      row.append(element("td", "name", item.item_name));
      row.append(element("td", "quantity", item.remaining_quantity));
      const field = element("td", "field");
      field.append(quantityField(state, key, { max: item.remaining_quantity, handlers }));
      row.append(field);
      const controls = element("td", "controls");
      controls.append(
        button("Claim", {
          style: "primary",
          busy: state.busy,
          onPress: () => handlers.claim(item, amountIn(state, key)),
        }),
      );
      row.append(controls);
      body.append(row);
    }
    block.append(listing);
    screen.append(block);
  }
  return screen;
}

/** The four fields a coin amount is typed into, and what they add up to. */
function coinFields(state, handlers, prefix) {
  const row = element("div", "coin-fields");
  for (const denomination of ["cp", "sp", "gp", "pp"]) {
    const key = `${prefix}:${denomination}`;
    const label = element("label", "coin-field");
    label.append(element("span", "muted", denomination));
    const node = element("input", "quantity-field");
    node.type = "number";
    node.min = "0";
    node.value = state.inputs[key] ?? "";
    node.placeholder = "0";
    node.dataset.inputKey = key;
    node.addEventListener("input", () => handlers.setInput(key, node.value));
    label.append(node);
    row.append(label);
  }
  return row;
}

function coinAmounts(state, prefix) {
  const amounts = {};
  for (const denomination of ["cp", "sp", "gp", "pp"]) {
    const typed = Number.parseInt(state.inputs[`${prefix}:${denomination}`] ?? "", 10);
    if (Number.isFinite(typed) && typed > 0) amounts[denomination] = typed;
  }
  return amounts;
}

function renderTreasuryScreen(state, handlers) {
  const screen = element("section", "screen");
  const treasury = state.treasury || { treasury: {}, purse: { character: null, balance: {} } };
  screen.append(element("h2", null, "The party's money"));
  screen.append(element("p", "coin big", formatCoin(treasury.treasury)));

  const purse = treasury.purse;
  if (purse.character) {
    screen.append(element("h2", null, `${purse.character.name} is carrying`));
    screen.append(element("p", "coin big", formatCoin(purse.balance)));
    const form = element("div", "form");
    form.append(renderDestination(state, handlers, "coin-destination"));
    form.append(coinFields(state, handlers, "coin"));
    form.append(
      button("Give coin", {
        style: "primary",
        busy: state.busy,
        onPress: () =>
          handlers.returnCoin(coinAmounts(state, "coin"), state.inputs["coin-destination"] ?? "party"),
      }),
    );
    screen.append(form);
  } else {
    screen.append(element("p", "muted", "You have no active character, so you are carrying nothing."));
  }

  if (state.actor?.isDm) {
    screen.append(renderTreasuryGive(state, handlers));
  }
  return screen;
}

/** Treasury → a character. A DM control on the panel, and one here. */
function renderTreasuryGive(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Give from the treasury"));
  const recipients = (state.roster || []).filter((character) => character.lifecycle === "ACTIVE");
  if (recipients.length === 0) {
    block.append(element("p", "muted", "Nobody is registered to receive it."));
    return block;
  }
  const form = element("div", "form");
  const wrap = element("label", "destination");
  wrap.append(element("span", "muted", "To"));
  const select = element("select", "select");
  select.dataset.inputKey = "treasury-recipient";
  for (const character of recipients) {
    const option = element("option", null, character.name);
    option.value = character.id;
    option.selected = state.inputs["treasury-recipient"] === character.id;
    select.append(option);
  }
  select.addEventListener("change", () => handlers.setInput("treasury-recipient", select.value));
  wrap.append(select);
  form.append(wrap);
  form.append(coinFields(state, handlers, "grant-coin"));
  form.append(
    button("Give coin", {
      style: "primary",
      busy: state.busy,
      onPress: () => {
        const id = state.inputs["treasury-recipient"] || recipients[0].id;
        handlers.giveCoin({ id }, coinAmounts(state, "grant-coin"));
      },
    }),
  );
  block.append(form);
  return block;
}

const SCREEN_BODIES = {
  stash: renderStashScreen,
  items: renderItemsScreen,
  loot: renderLootScreen,
  treasury: renderTreasuryScreen,
};

export function renderApp(state, handlers) {
  const root = element("div", "layout");

  const header = element("header");
  const title = element("div", "title");
  title.append(element("h1", null, "Quartermaster"), renderLive(state.live));
  header.append(title);
  header.append(renderSummary(state.home));
  header.append(renderTabs(state, handlers));
  root.append(header);

  const main = element("main");
  const notice = renderNotice(state, handlers);
  if (notice) main.append(notice);
  const prompt = renderPrompt(state, handlers);
  if (prompt) main.append(prompt);
  main.append((SCREEN_BODIES[state.screen] || renderStashScreen)(state, handlers));
  root.append(main);

  root.append(renderRoster(state, handlers));
  return root;
}
