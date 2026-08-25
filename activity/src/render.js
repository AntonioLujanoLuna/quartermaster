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
function quantityField(state, key, { max, handlers, initial = "1" }) {
  const node = element("input", "quantity-field");
  node.type = "number";
  node.min = "1";
  if (max !== undefined) node.max = String(max);
  node.value = state.inputs[key] ?? initial;
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

/**
 * How many item rows the drop form is showing.
 *
 * Held in `state.inputs` with everything else that has been typed but not
 * sent, so a live change redrawing the screen does not take the DM's half-
 * filled second row away with it.
 */
export const DROP_ROW_LIMIT = 50;

function dropRowCount(state) {
  const typed = Number.parseInt(state.inputs["drop:rows"] ?? "", 10);
  return Number.isFinite(typed) && typed > 0 ? Math.min(typed, DROP_ROW_LIMIT) : 1;
}

/** A field with a word in front of it, which is most of the DM forms. */
function labelled(text, field) {
  const wrap = element("label", "field-label");
  wrap.append(element("span", "muted", text), field);
  return wrap;
}

function amountIn(state, key, fallback = 1) {
  const typed = Number.parseInt(state.inputs[key] ?? "", 10);
  return Number.isFinite(typed) && typed > 0 ? typed : fallback;
}

/**
 * Narrow a list to what somebody is looking for.
 *
 * The panels truncated because Discord gave them twenty-five controls and two
 * thousand characters. This surface has neither bound and says so — it
 * scrolls — but a stash forty stacks deep still answers "have we got rope" by
 * making somebody read all forty. Filtering is that question asked once.
 *
 * It matches the provenance as well as the name, because half of what the
 * table wants to find again it remembers by where it came from rather than by
 * what it is called.
 */
function filterValue(state, key) {
  return (state.inputs[key] ?? "").trim().toLowerCase();
}

function matchesFilter(needle, ...fields) {
  if (!needle) return true;
  return fields.some((field) => {
    const text = String(field ?? "").toLowerCase();
    return text.includes(needle);
  });
}

/**
 * The field itself.
 *
 * Unlike every other input on these screens it redraws on each keystroke,
 * because the list underneath it *is* the answer and a filter that only
 * applies once you look away is not one. `draw` puts the caret back where it
 * was, which is the mechanism that already exists for the live feed redrawing
 * under somebody who is typing.
 */
function filterField(state, key, placeholder, handlers) {
  const wrap = element("div", "filter");
  const node = element("input", "text-field");
  node.type = "search";
  node.placeholder = placeholder;
  node.value = state.inputs[key] ?? "";
  node.dataset.inputKey = key;
  node.setAttribute("aria-label", placeholder);
  node.addEventListener("input", () => handlers.setFilter(key, node.value));
  wrap.append(node);
  return wrap;
}

/**
 * How many rows the reader is looking at, and how many exist.
 *
 * A filtered list that only says "3 stacks" has quietly become a lie about the
 * campaign, so the total stays and the shown count joins it.
 */
function countLine(shown, total, noun) {
  const plural = total === 1 ? noun : `${noun}s`;
  const text = shown === total ? `${total} ${plural}` : `${shown} of ${total} ${plural} shown`;
  return element("p", "count muted", text);
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

function renderContinuity(continuity) {
  if (!continuity || continuity.active_session_number !== null || !continuity.previous) return null;

  const section = element("section", "continuity");
  section.append(element("h2", null, "Last time"));
  const endpoint = continuity.previous.where_ended
    ? `Ended at: ${continuity.previous.where_ended}`
    : "No endpoint was recorded.";
  section.append(element("p", null, endpoint));

  const recap = Array.isArray(continuity.recap) ? continuity.recap : [];
  if (recap.length > 0) {
    const list = element("ol", "continuity-recap");
    for (const line of recap) list.append(element("li", null, line));
    section.append(list);
  } else {
    section.append(element("p", "muted", "No recorded history for that session."));
  }

  const earlier = Math.max(0, Number(continuity.recap_total || 0) - recap.length);
  if (earlier > 0) {
    section.append(
      element(
        "p",
        "muted",
        `${earlier} earlier ${earlier === 1 ? "line" : "lines"} not shown; the session log and export hold the full record.`,
      ),
    );
  }
  return section;
}

const SCREENS = [
  { id: "stash", label: "Party Stash" },
  { id: "items", label: "My Items" },
  // Between "what we share" and "what I carry" sits "who has the rope", which
  // until now only the DM-only export could answer.
  { id: "party", label: "Who Has What" },
  { id: "dossier", label: "Character" },
  { id: "loot", label: "Loot" },
  { id: "treasury", label: "Treasury" },
  { id: "dice", label: "Dice" },
  // Stage 5. Grant, correct, drops, adjust, and split live on the screens that
  // already show what they change, because that is where the DM is looking
  // when they reach for them. This tab is what has no such screen: the
  // session, the fight, the roster's lifecycle, and the operator's controls.
  { id: "dm", label: "DM", dm: true },
];

function renderTabs(state, handlers) {
  const nav = element("nav", "tabs");
  for (const screen of SCREENS) {
    if (screen.dm && !state.actor?.isDm) continue;
    const tab = element(
      "button",
      state.screen === screen.id ? "tab tab-current" : "tab",
      screen.label,
    );
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

function modifierText(modifier) {
  if (!modifier) return "0";
  return modifier > 0 ? `+${modifier}` : String(modifier);
}

function renderDiceResult(result) {
  const block = element("article", "dice-result");
  const title = result.label || result.expression;
  block.append(element("h3", null, title));
  const mode = result.mode === "normal" ? "normal" : result.mode;
  block.append(element("p", "muted", `${result.expression} · ${mode}`));

  const breakdown = element("ul", "dice-breakdown");
  for (const [index, die] of (result.dice || []).entries()) {
    const selected = result.selected === null || result.selected === index;
    const values = Array.isArray(die.values) ? die.values.join(", ") : "?";
    breakdown.append(
      element(
        "li",
        selected ? "dice-selected" : "dice-discarded",
        `d${die.sides}: [${values}]${selected ? " · counted" : " · not counted"}`,
      ),
    );
  }
  block.append(breakdown);
  block.append(element("p", "muted", `Modifier ${modifierText(result.modifier)}`));
  block.append(element("p", "dice-total", `Total ${result.total}`));
  if (result.natural !== null && result.natural !== undefined) {
    block.append(element("p", "muted", `Natural ${result.natural}`));
  }
  if (result.recorded === false) {
    block.append(
      element("p", "dice-status dice-private", "Private roll · not added to the session log."),
    );
  } else if (result.recorded === true || result.visibility === "PUBLIC") {
    block.append(element("p", "dice-status", "Recorded in the session log."));
  }
  return block;
}

function renderDiceScreen(state, handlers) {
  const screen = element("section", "screen");
  screen.append(element("h2", null, "Dice"));
  screen.append(
    element(
      "p",
      "muted",
      "Rolls show every die, modifier, and selected result. Use Avrae for attacks, spells, and character mechanics.",
    ),
  );

  const presets = element("div", "dice-presets");
  presets.setAttribute("role", "group");
  presets.setAttribute("aria-label", "Common rolls");
  presets.append(element("span", "dice-presets-label", "Common rolls"));
  for (const preset of [
    { label: "d20", expression: "d20", mode: "normal", title: "Fill the form with a normal d20" },
    {
      label: "Advantage",
      expression: "d20",
      mode: "advantage",
      title: "Fill the form with an advantage d20",
    },
    {
      label: "Disadvantage",
      expression: "d20",
      mode: "disadvantage",
      title: "Fill the form with a disadvantage d20",
    },
  ]) {
    presets.append(
      button(preset.label, {
        style: "quiet",
        busy: state.busy,
        title: preset.title,
        onPress: () => handlers.setDicePreset(preset.expression, preset.mode),
      }),
    );
  }
  screen.append(presets);

  const form = element("div", "form dice-form");
  form.append(labelled("Roll", textField(state, "dice:expression", "d20 or 2d6+3", handlers)));

  const modeLabel = element("label", "field-label");
  modeLabel.append(element("span", "muted", "Mode"));
  const mode = element("select", "select");
  mode.dataset.inputKey = "dice:mode";
  for (const [value, label] of [
    ["normal", "Normal"],
    ["advantage", "Advantage"],
    ["disadvantage", "Disadvantage"],
  ]) {
    const option = element("option", null, label);
    option.value = value;
    option.selected = (state.inputs["dice:mode"] || "normal") === value;
    mode.append(option);
  }
  mode.addEventListener("change", () => handlers.setInput("dice:mode", mode.value));
  modeLabel.append(mode);
  form.append(modeLabel);

  form.append(labelled("Label", textField(state, "dice:label", "Strength check", handlers)));

  const visibilityLabel = element("label", "field-label");
  visibilityLabel.append(element("span", "muted", "Show"));
  const visibility = element("select", "select");
  visibility.dataset.inputKey = "dice:visibility";
  const visibilityOptions = [{ value: "PUBLIC", label: "Public" }];
  if (state.actor?.isDm) visibilityOptions.push({ value: "DM_ONLY", label: "DM only" });
  for (const optionValue of visibilityOptions) {
    const option = element("option", null, optionValue.label);
    option.value = optionValue.value;
    option.selected = (state.inputs["dice:visibility"] || "PUBLIC") === option.value;
    visibility.append(option);
  }
  visibility.addEventListener("change", () =>
    handlers.setInput("dice:visibility", visibility.value),
  );
  visibilityLabel.append(visibility);
  form.append(visibilityLabel);
  form.append(
    button("Roll", {
      style: "primary",
      busy: state.busy,
      onPress: () =>
        handlers.rollDice(
          state.inputs["dice:expression"] || "",
          state.inputs["dice:mode"] || "normal",
          state.inputs["dice:label"] || "",
          state.inputs["dice:visibility"] || "PUBLIC",
        ),
    }),
  );
  screen.append(form);
  screen.append(
    element(
      "p",
      "dice-guidance",
      "Bonuses are explicit in the Roll field, for example d20+5. Automatic character bonuses will only appear after a verified character-sheet source is connected.",
    ),
  );

  if (state.dice?.last) {
    screen.append(element("h2", null, "Last roll"));
    screen.append(renderDiceResult(state.dice.last));
  }

  screen.append(element("h2", null, "Public rolls"));
  const rolls = state.dice?.rolls || [];
  if (rolls.length === 0) {
    screen.append(element("p", "muted", "No public rolls have been recorded yet."));
  } else {
    const history = element("div", "dice-rolls");
    for (const result of rolls) history.append(renderDiceResult(result));
    screen.append(history);
  }
  return screen;
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
  const needle = filterValue(state, "filter:stash");
  const shown = stash.items.filter((item) =>
    matchesFilter(needle, item.item_name, item.provenance),
  );
  screen.append(filterField(state, "filter:stash", "Filter the stash", handlers));
  const [listing, body] = table(["Item", "Qty", "Where it came from", ""]);
  // No truncation and no "dropped N entries". The list scrolls, which is the
  // whole reason this surface exists.
  for (const item of shown) {
    const row = element("tr");
    row.append(element("td", "name", item.item_name));
    row.append(element("td", "quantity", item.quantity));
    row.append(element("td", "provenance muted", item.provenance || "—"));
    const controls = element("td", "controls");
    controls.append(button("Take 1", { busy: state.busy, onPress: () => handlers.take(item, 1) }));
    if (item.quantity > 1) {
      controls.append(
        button("Take all", {
          style: "primary",
          busy: state.busy,
          onPress: () => handlers.take(item, "all"),
        }),
      );
    }
    if (state.actor?.isDm) {
      // The repair for the most likely mistake at the table: a stack granted
      // with the wrong number on it. It removes rather than moves, so it asks.
      controls.append(
        button("Correct…", {
          style: "danger",
          busy: state.busy,
          title: "Removes it from the campaign. Not a transfer.",
          onPress: () => handlers.correct(item),
        }),
      );
    }
    row.append(controls);
    body.append(row);
  }
  if (shown.length === 0) {
    body.append(
      emptyRow(
        4,
        stash.items.length === 0
          ? "The Party Stash is empty."
          : "Nothing in the stash matches that.",
      ),
    );
  }
  screen.append(listing);
  screen.append(countLine(shown.length, stash.total, "stack"));
  if (!state.home?.character) {
    screen.append(
      element(
        "p",
        "count muted",
        "Taking something needs a registered character. Ask the DM to register one.",
      ),
    );
  }
  if (state.actor?.isDm) screen.append(renderGrant(state, handlers));
  return screen;
}

/** Put something in. The one mutation that mints rather than moves. */
function renderGrant(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Grant to the Party Stash"));
  const form = element("div", "form");
  form.append(labelled("Item", textField(state, "grant:item", "Silvered dagger", handlers)));
  form.append(labelled("How many", quantityField(state, "grant:quantity", { handlers })));
  // The only field on this surface that can say where something came from:
  // nothing upstream of a grant knows, so if it is not typed here it is lost.
  form.append(
    labelled("Where from", textField(state, "grant:provenance", "The dragon's hoard", handlers)),
  );
  form.append(
    button("Grant", {
      style: "primary",
      busy: state.busy,
      onPress: () =>
        handlers.grant(
          state.inputs["grant:item"] ?? "",
          amountIn(state, "grant:quantity"),
          state.inputs["grant:provenance"] ?? "",
        ),
    }),
  );
  block.append(form);
  return block;
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

  const needle = filterValue(state, "filter:items");
  const shown = holdings.items.filter((item) =>
    matchesFilter(needle, item.item_name, item.provenance),
  );
  screen.append(filterField(state, "filter:items", "Filter what you are carrying", handlers));

  // "Where it came from" travels with the stack and the Party Stash has always
  // shown it. Not showing it here made the same fact answerable about what the
  // party shares and unanswerable about what a player is holding, which is the
  // half somebody actually has to account for.
  const [listing, body] = table(["Item", "Held", "Where it came from", "How many", ""]);
  for (const item of shown) {
    const key = `give:${item.id}`;
    const row = element("tr");
    row.append(element("td", "name", item.item_name));
    row.append(element("td", "quantity", item.quantity));
    row.append(element("td", "provenance muted", item.provenance || "—"));
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
  if (shown.length === 0) {
    body.append(
      emptyRow(
        5,
        holdings.items.length === 0
          ? `${holdings.character.name} is carrying nothing.`
          : "Nothing you are carrying matches that.",
      ),
    );
  }
  screen.append(listing);
  const count = countLine(shown.length, holdings.total_items, "stack");
  count.textContent = `${holdings.character.name} · ${count.textContent}`;
  screen.append(count);
  return screen;
}

/**
 * Who has the rope.
 *
 * The Party Stash screen answers what the party shares and My Items answers
 * what you are carrying. Between them sat the question the table actually asks
 * out loud, and until now the only surface that named a holder was the export,
 * which is a DM-only document and a wall of prose.
 *
 * It reads rather than acts. Moving somebody else's property is not a thing
 * the domain allows — a give is made by the character holding the stack — so
 * there is nothing here to press, and the screen does not pretend otherwise.
 */
function renderPartyScreen(state, handlers) {
  const screen = element("section", "screen");
  const party = state.party;
  if (!party) {
    screen.append(element("p", "muted", "Reading what everyone is carrying…"));
    return screen;
  }

  const needle = filterValue(state, "filter:party");
  screen.append(filterField(state, "filter:party", "Filter by item, holder, or origin", handlers));

  const [listing, body] = table(["Item", "Qty", "Held by", "Where it came from"]);
  let shown = 0;
  for (const holder of party.characters) {
    for (const item of holder.items) {
      if (!matchesFilter(needle, item.item_name, holder.character_name, item.provenance)) continue;
      shown += 1;
      const row = element("tr");
      row.append(element("td", "name", item.item_name));
      row.append(element("td", "quantity", item.quantity));
      const holderCell = element("td", null, holder.character_name);
      if (holder.lifecycle !== "ACTIVE") {
        // A stack still held by somebody who has stopped playing is exactly
        // what estate resolution is for, and seeing it is how a DM knows one
        // is outstanding.
        holderCell.append(element("span", "muted", ` · ${holder.lifecycle.toLowerCase()}`));
      }
      row.append(holderCell);
      row.append(element("td", "provenance muted", item.provenance || "—"));
      body.append(row);
    }
  }
  if (shown === 0) {
    body.append(
      emptyRow(
        4,
        party.total_stacks === 0
          ? "Nobody is carrying anything yet."
          : "Nothing anybody is carrying matches that.",
      ),
    );
  }
  screen.append(listing);
  screen.append(countLine(shown, party.total_stacks, "stack"));
  screen.append(
    element(
      "p",
      "count muted",
      "What the party shares is on the Party Stash screen. This is what people are holding.",
    ),
  );
  return screen;
}

function renderDossierScreen(state) {
  const screen = element("section", "screen");
  const dossier = state.dossier;
  if (!dossier) {
    screen.append(element("p", "muted", "Reading the character snapshot…"));
    return screen;
  }
  if (dossier.status === "UNAVAILABLE" || !dossier.snapshot) {
    screen.append(element("h2", null, "Character dossier"));
    screen.append(element("p", "dossier-status dossier-unavailable", dossier.reason));
    if (dossier.character) {
      screen.append(
        element("p", "muted", `${dossier.character.name} has no imported sheet snapshot yet.`),
      );
    }
    return screen;
  }

  const snapshot = dossier.snapshot;
  const character = dossier.character;
  screen.append(element("h2", null, `${character?.name || "Character"} dossier`));
  screen.append(
    element(
      "p",
      `dossier-status dossier-${dossier.status.toLowerCase()}`,
      dossier.status === "CURRENT" ? "Current snapshot" : "Stale snapshot",
    ),
  );
  screen.append(
    element(
      "p",
      "muted",
      `${snapshot.system} · rules ${snapshot.rules_version} · source ${snapshot.source} · observed ${snapshot.observed_at}`,
    ),
  );
  if (snapshot.source_reference)
    screen.append(element("p", "muted", `Source reference: ${snapshot.source_reference}`));

  const [listing, body] = table(["Value", "Reading", "Source"]);
  const add = (label, value) => {
    const row = element("tr");
    row.append(element("td", "name", label));
    row.append(
      element("td", "quantity", value === null || value === undefined ? "Not supplied" : value),
    );
    row.append(element("td", "muted", "Imported snapshot"));
    body.append(row);
  };
  add("Level", snapshot.level);
  add("Proficiency bonus", snapshot.proficiency_bonus);
  add("Armor Class", snapshot.armor_class);
  add("Hit points", snapshot.hit_points);
  add("Temporary hit points", snapshot.temporary_hit_points);
  add("Initiative", snapshot.initiative);
  add("Spell attack modifier", snapshot.spell_attack_modifier);
  add("Spell save DC", snapshot.spell_save_dc);
  for (const [name, value] of Object.entries(snapshot.ability_modifiers || {}))
    add(`${name} modifier`, value);
  for (const [name, value] of Object.entries(snapshot.saving_throws || {}))
    add(`${name} save`, value);
  screen.append(listing);

  const details = element("div", "dossier-details");
  const detail = (title, values) => {
    const block = element("div", "dossier-block");
    block.append(element("h3", null, title));
    const entries = Object.entries(values || {});
    if (entries.length === 0) block.append(element("p", "muted", "Not supplied."));
    for (const [name, value] of entries) block.append(element("p", null, `${name}: ${value}`));
    details.append(block);
  };
  detail("Ability scores", snapshot.ability_scores);
  detail("Spell resources", snapshot.spell_resources);
  detail("Equipped", snapshot.equipped);
  screen.append(details);
  screen.append(
    element(
      "p",
      "muted",
      "This dossier explains a supplied snapshot. It does not authorize or calculate a mechanic.",
    ),
  );
  return screen;
}

function renderLootScreen(state, handlers) {
  const screen = element("section", "screen");
  const drops = state.loot?.drops || [];
  if (drops.length === 0) {
    screen.append(element("p", "muted", "No Loot Drop is open."));
    if (state.actor?.isDm) screen.append(renderDropForm(state, handlers));
    return screen;
  }
  for (const drop of drops) {
    const block = element("div", "drop");
    const heading = element("div", "drop-heading");
    heading.append(element("h2", null, `Loot Drop · closes ${drop.expires_at}`));
    if (state.actor?.isDm) {
      // Closing early is not destructive: whatever nobody claimed goes back to
      // the Party Stash, which is where it would have gone at expiry anyway.
      heading.append(
        button("Close it", {
          style: "quiet",
          busy: state.busy,
          title: "What is left goes back to the Party Stash.",
          onPress: () => handlers.closeDrop(drop.drop_id),
        }),
      );
    }
    block.append(heading);
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
  if (state.actor?.isDm) screen.append(renderDropForm(state, handlers));
  return screen;
}

/**
 * Open a Loot Drop.
 *
 * The panel's drop holds one item, because a Discord modal holds five fields
 * and three of them were already spoken for. A form is not a modal, so this
 * one takes a list — which is what a pile of loot from one fight actually is.
 */
function renderDropForm(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Open a Loot Drop"));
  const rows = dropRowCount(state);
  const [listing, body] = table(["Item", "How many", "Where from"]);
  for (let index = 0; index < rows; index += 1) {
    const row = element("tr");
    for (const [suffix, placeholder] of [
      ["item", "Loot gem"],
      [null, null],
      ["provenance", "Off the ogre"],
    ]) {
      const cell = element("td");
      cell.append(
        suffix === null
          ? quantityField(state, `drop:${index}:quantity`, { handlers })
          : textField(state, `drop:${index}:${suffix}`, placeholder, handlers),
      );
      row.append(cell);
    }
    body.append(row);
  }
  block.append(listing);
  const form = element("div", "form");
  form.append(button("Another item", { style: "quiet", onPress: () => handlers.addDropRow() }));
  form.append(
    labelled("Closes in (hours)", quantityField(state, "drop:expiry", { handlers, initial: "72" })),
  );
  form.append(
    button("Open the drop", {
      style: "primary",
      busy: state.busy,
      onPress: () => handlers.createDrop(rows),
    }),
  );
  block.append(form);
  return block;
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

/** The same four fields, but a negative number is a legitimate answer. */
function signedCoinFields(state, handlers, prefix) {
  const row = element("div", "coin-fields");
  for (const denomination of ["cp", "sp", "gp", "pp"]) {
    const key = `${prefix}:${denomination}`;
    const label = element("label", "coin-field");
    label.append(element("span", "muted", denomination));
    const node = element("input", "quantity-field");
    node.type = "number";
    node.value = state.inputs[key] ?? "";
    node.placeholder = "0";
    node.dataset.inputKey = key;
    node.addEventListener("input", () => handlers.setInput(key, node.value));
    label.append(node);
    row.append(label);
  }
  return row;
}

function signedCoinAmounts(state, prefix) {
  const deltas = {};
  for (const denomination of ["cp", "sp", "gp", "pp"]) {
    const typed = Number.parseInt(state.inputs[`${prefix}:${denomination}`] ?? "", 10);
    if (Number.isFinite(typed) && typed !== 0) deltas[denomination] = typed;
  }
  return deltas;
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
          handlers.returnCoin(
            coinAmounts(state, "coin"),
            state.inputs["coin-destination"] ?? "party",
          ),
      }),
    );
    screen.append(form);
  } else {
    screen.append(
      element("p", "muted", "You have no active character, so you are carrying nothing."),
    );
  }

  if (state.actor?.isDm) {
    screen.append(renderTreasuryGive(state, handlers));
    screen.append(renderTreasuryAdjust(state, handlers));
    screen.append(renderTreasurySplit(state, handlers));
  }
  return screen;
}

/** Correct the treasury, by a signed amount per denomination. */
function renderTreasuryAdjust(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Adjust the treasury"));
  block.append(
    element("p", "muted", "Negative takes it out. Nothing here moves coin to or from a character."),
  );
  const form = element("div", "form");
  form.append(signedCoinFields(state, handlers, "adjust"));
  form.append(labelled("Why", textField(state, "adjust:reason", "Paid the ferryman", handlers)));
  form.append(
    button("Adjust", {
      style: "primary",
      busy: state.busy,
      onPress: () =>
        handlers.adjustTreasury(
          signedCoinAmounts(state, "adjust"),
          state.inputs["adjust:reason"] ?? "",
        ),
    }),
  );
  block.append(form);
  return block;
}

/**
 * Split the treasury among everyone who is alive.
 *
 * Pressing this shows the shares and waits, rather than paying them: the
 * share depends on how many characters are active, and the DM is entitled to
 * see who is being paid what before any coin moves.
 */
function renderTreasurySplit(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Split the treasury"));
  const active = (state.roster || []).filter(
    (character) => character.lifecycle === "ACTIVE",
  ).length;
  block.append(
    element(
      "p",
      "muted",
      active === 0
        ? "Nobody is active, so there is nobody to split it among."
        : `Among ${active} active ${active === 1 ? "character" : "characters"}. Each denomination ` +
            "divides on its own, and what will not divide stays in the treasury.",
    ),
  );
  const form = element("div", "form");
  form.append(coinFields(state, handlers, "split"));
  form.append(
    button("Preview the split", {
      style: "primary",
      busy: state.busy,
      onPress: () => handlers.split(coinAmounts(state, "split")),
    }),
  );
  block.append(form);
  return block;
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

// The DM screen ---------------------------------------------------------------
//
// What the other four screens have no room for, because it is not about a list
// of things: the session, the fight, the roster's lifecycle, and the controls
// that are about the runtime rather than the campaign.

function renderDmScreen(state, handlers) {
  const screen = element("section", "screen");
  if (!state.actor?.isDm) {
    // Reachable only by a token that stopped saying DM while the tab was open.
    // The API refuses everything on this screen either way; this is so the
    // screen says why rather than answering every press with a 403.
    screen.append(element("p", "muted", "This is the DM's screen, and you are not the DM."));
    return screen;
  }
  screen.append(renderSessionBlock(state, handlers));
  screen.append(renderCombatBlock(state, handlers));
  screen.append(renderRosterBlock(state, handlers));
  screen.append(renderMaintenanceBlock(state, handlers));
  return screen;
}

function renderSessionBlock(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "The session"));
  const running = state.home?.active_session_number ?? null;
  if (running === null) {
    const previous = state.home?.previous_session;
    block.append(
      element(
        "p",
        "muted",
        previous?.where_ended
          ? `Last time ended: ${previous.where_ended}`
          : "No session has been ended with an endpoint yet.",
      ),
    );
    block.append(
      button("Start a session", {
        style: "primary",
        busy: state.busy,
        onPress: () => handlers.startSession(),
      }),
    );
    return block;
  }
  block.append(element("p", null, `Session ${running} is running.`));
  const form = element("div", "form");
  // Required, here as on the panel. It is the whole of the continuity the next
  // evening opens on, and a session ended without one leaves nothing to pick
  // up — so it is a field on the screen rather than a prompt after the press.
  form.append(
    labelled("Where did it end?", textField(state, "end:where", "The Sunken Tomb", handlers)),
  );
  form.append(
    button("End the session", {
      style: "danger",
      busy: state.busy,
      title: "Closes any open Loot Drops and any open fight.",
      onPress: () => handlers.endSession(state.inputs["end:where"] ?? ""),
    }),
  );
  block.append(form);
  return block;
}

function renderCombatBlock(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Combat"));
  const combat = state.combat;
  if (!combat) {
    // The DM screen is drawn before its read lands, and "start a session
    // first" is the wrong thing to say to somebody who has one running.
    block.append(element("p", "muted", "Reading…"));
    return block;
  }
  if (combat.status === "NO_ACTIVE_SESSION") {
    block.append(element("p", "muted", "A fight belongs to a session. Start one first."));
    return block;
  }
  // Quartermaster's own record and nothing Avrae owns: when it started, how
  // long it has run, and how it ended. The mechanics stay where they are.
  if (combat.status === "OPEN") {
    block.append(
      element(
        "p",
        "combat-source",
        "Quartermaster records the encounter. Avrae owns initiative, attacks, spells, saves, HP, and conditions.",
      ),
    );
    block.append(
      element(
        "p",
        null,
        `A fight has been open for ${Math.round(combat.encounter.elapsed_seconds)}s.`,
      ),
    );
    const form = element("div", "form");
    form.append(
      labelled("How did it end?", textField(state, "combat:outcome", "The ogre fled", handlers)),
    );
    form.append(
      button("End combat", {
        style: "primary",
        busy: state.busy,
        onPress: () => handlers.closeCombat(state.inputs["combat:outcome"] ?? ""),
      }),
    );
    block.append(form);
    return block;
  }
  if (combat.last_closed?.outcome) {
    block.append(element("p", "muted", `Last fight: ${combat.last_closed.outcome}`));
  }
  block.append(
    button("Start combat", {
      style: "primary",
      busy: state.busy,
      onPress: () => handlers.openCombat(),
    }),
  );
  return block;
}

const LIFECYCLES = ["ACTIVE", "DEAD", "RETIRED", "DEPARTED"];

/**
 * The roster, and the two things a DM does to it that are not registration.
 *
 * A lifecycle change never moves anything — that is the invariant, and it is
 * why resolving belongings is a second control rather than a consequence of
 * the first.
 */
function renderRosterBlock(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Characters"));
  const roster = state.roster || [];
  if (roster.length === 0) {
    block.append(
      element("p", "muted", "Nobody is registered. Register from the roster beside this."),
    );
    return block;
  }
  const [listing, body] = table(["Character", "State", "Change to", "Belongings"]);
  for (const character of roster) {
    const row = element("tr");
    row.append(element("td", "name", character.name));
    row.append(element("td", null, character.lifecycle.toLowerCase()));

    const change = element("td", "controls");
    const select = element("select", "select");
    const lifecycleKey = `lifecycle:${character.id}`;
    select.dataset.inputKey = lifecycleKey;
    for (const lifecycle of LIFECYCLES) {
      const option = element("option", null, lifecycle.toLowerCase());
      option.value = lifecycle;
      option.selected = (state.inputs[lifecycleKey] ?? character.lifecycle) === lifecycle;
      select.append(option);
    }
    select.addEventListener("change", () => handlers.setInput(lifecycleKey, select.value));
    change.append(select);
    change.append(
      button("Apply", {
        busy: state.busy,
        onPress: () =>
          handlers.transition(character, state.inputs[lifecycleKey] ?? character.lifecycle),
      }),
    );
    row.append(change);

    const estate = element("td", "controls");
    if (character.lifecycle === "ACTIVE") {
      // Only a character who has stopped playing has an estate to resolve, and
      // the domain refuses the rest. Not offering it is the same answer,
      // earlier.
      estate.append(element("span", "muted", "—"));
    } else {
      const destinationKey = `estate:${character.id}`;
      const destination = element("select", "select");
      destination.dataset.inputKey = destinationKey;
      const options = [{ value: "party", label: "The Party Stash" }];
      for (const other of roster) {
        if (other.lifecycle !== "ACTIVE") continue;
        options.push({ value: other.id, label: other.name });
      }
      for (const option of options) {
        const node = element("option", null, option.label);
        node.value = option.value;
        node.selected = (state.inputs[destinationKey] ?? "party") === option.value;
        destination.append(node);
      }
      destination.addEventListener("change", () =>
        handlers.setInput(destinationKey, destination.value),
      );
      estate.append(destination);
      estate.append(
        button("Resolve", {
          busy: state.busy,
          onPress: () => handlers.resolveEstate(character, state.inputs[destinationKey] ?? "party"),
        }),
      );
    }
    row.append(estate);
    body.append(row);
  }
  block.append(listing);
  return block;
}

function renderMaintenanceBlock(state, handlers) {
  const block = element("div", "dm-block");
  block.append(element("h2", null, "Maintenance"));
  block.append(
    element(
      "p",
      "muted",
      "The export is the full record every surface points at, and the one to read during an outage.",
    ),
  );
  const form = element("div", "form");
  form.append(button("Health", { busy: state.busy, onPress: () => handlers.health() }));
  form.append(button("Back up", { busy: state.busy, onPress: () => handlers.backup() }));
  form.append(
    button("Run maintenance", { busy: state.busy, onPress: () => handlers.runMaintenance() }),
  );
  form.append(
    button("Export", { style: "quiet", busy: state.busy, onPress: () => handlers.export() }),
  );
  block.append(form);
  if (state.report) {
    // A pre rather than a paragraph: this is the operator's text, and it is
    // laid out in columns that a reflow would take apart.
    block.append(element("pre", "report", state.report));
    block.append(button("Close", { style: "quiet", onPress: handlers.dismissReport }));
  }
  return block;
}

const SCREEN_BODIES = {
  stash: renderStashScreen,
  items: renderItemsScreen,
  party: renderPartyScreen,
  dossier: renderDossierScreen,
  loot: renderLootScreen,
  treasury: renderTreasuryScreen,
  dice: renderDiceScreen,
  dm: renderDmScreen,
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
  const continuity = renderContinuity(state.continuity);
  if (continuity) main.append(continuity);
  const notice = renderNotice(state, handlers);
  if (notice) main.append(notice);
  const prompt = renderPrompt(state, handlers);
  if (prompt) main.append(prompt);
  main.append((SCREEN_BODIES[state.screen] || renderStashScreen)(state, handlers));
  root.append(main);

  root.append(renderRoster(state, handlers));
  return root;
}
