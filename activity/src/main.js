// Stages 2, 3, 4, and 5 of docs/activity-migration-plan.md: the walking
// skeleton, live, able to act, and able to run an evening.
//
// Stage 2 proved the handshake, the proxy, and the hosting. Stage 3 made the
// table look at one thing rather than six copies of one thing. Stage 4 is the
// point of the exercise: a player takes, gives, uses, claims, and hands over
// coin from here, and everyone else's screen says so without anyone pressing
// anything. Stage 5 is the DM's half — the session, the loot, the treasury,
// the roster, and the fight — so that an evening does not need a panel.
//
// This module holds the boot sequence and the state the screens read. What a
// press means is in actions.js; what it looks like is in render.js.

import { DiscordSDK } from "@discord/embedded-app-sdk";
import { api, ApiError, onExpiry, setSessionToken, sessionTokenValue } from "./api.js";
import { createActions } from "./actions.js";
import { openLiveFeed } from "./live.js";
import { DROP_ROW_LIMIT, renderApp, renderError, renderStatus } from "./render.js";

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;

// Long enough to collapse the burst a single action produces — a grant is one
// event, but a session end is many — and short enough that nobody at the table
// perceives it as a delay.
const REFRESH_DEBOUNCE_MS = 150;

const app = document.getElementById("app");
const state = {
  actor: null,
  participants: [],
  screen: "stash",
  home: null,
  stash: null,
  holdings: null,
  // What every character is carrying. Read only by the screen that shows it,
  // like every other list here.
  party: null,
  loot: null,
  treasury: null,
  combat: null,
  dice: { rolls: [], last: null },
  continuity: null,
  // What `health` or `export` last printed. It is a document rather than a
  // notice: nobody reads a health report in the corner of a screen.
  report: null,
  roster: [],
  dossier: null,
  playing: new Set(),
  live: "connecting",
  // What has been typed but not yet sent. Held here rather than only in the
  // DOM because a live change redraws the screen underneath whoever is typing.
  inputs: {},
  notice: null,
  prompt: null,
  busy: false,
};

// Which control was pressed, so the screen can say *this* is what is happening
// rather than greying out everything at once. Held outside `state` because it
// describes the DOM rather than the campaign.
let pressed = null;
let noticeTimer = null;

let refreshTimer = null;
let refreshing = null;
// Held past boot for one reason: opening combat records which channel the
// fight is in, and Discord is the only thing that knows.
let discordSdk = null;

// Reads ----------------------------------------------------------------------

const READERS = {
  home: async () => {
    state.home = await api.home();
  },
  roster: async () => {
    state.roster = (await api.characters()).characters;
    // Who already has a character is what decides whether the DM is offered a
    // Register control beside a name, and the roster is the only thing that
    // knows. Registering a second active character for one player is refused
    // in the transaction either way; this keeps it from being offered.
    state.playing = new Set(
      state.roster
        .filter((character) => character.lifecycle === "ACTIVE" && character.discord_user_id)
        .map((character) => String(character.discord_user_id)),
    );
  },
  stash: async () => {
    state.stash = await api.stash();
  },
  holdings: async () => {
    state.holdings = await api.myItems();
  },
  party: async () => {
    state.party = await api.partyHoldings();
  },
  loot: async () => {
    state.loot = await api.loot();
  },
  treasury: async () => {
    state.treasury = await api.treasury();
  },
  combat: async () => {
    state.combat = await api.combat();
  },
  dossier: async () => {
    state.dossier = await api.dossier();
  },
  continuity: async () => {
    state.continuity = await api.continuity();
  },
  dice: async () => {
    const last = state.dice?.last ?? null;
    state.dice = { ...(await api.diceRolls()), last };
  },
};

// What each screen needs on top of the two every screen needs. Reading only
// what is on screen is what keeps a change to one stack from costing six
// clients four queries each.
const SCREEN_READS = {
  stash: ["stash"],
  items: ["holdings"],
  party: ["party"],
  dossier: ["dossier"],
  loot: ["loot"],
  treasury: ["treasury"],
  // The roster is read for every screen already; the fight is not, and it is
  // the only thing the DM screen needs that nothing else asks for.
  dm: ["combat"],
  dice: ["dice"],
};

async function refresh() {
  const wanted = new Set(["home", "continuity", "roster", ...(SCREEN_READS[state.screen] || [])]);
  await Promise.all([...wanted].map((name) => READERS[name]()));
  draw();
}

function scheduleRefresh() {
  if (refreshTimer) return;
  refreshTimer = setTimeout(() => {
    refreshTimer = null;
    // Serialized rather than overlapped: two reads in flight can land in
    // either order, and the older one would paint over the newer.
    refreshing = (refreshing || Promise.resolve()).then(refresh).catch((error) => {
      // A failed refresh is not a dead screen. The socket is still open, so
      // the next change is another chance, and what is rendered is still the
      // last state that was actually read.
      console.warn("Quartermaster could not refresh", error);
    });
  }, REFRESH_DEBOUNCE_MS);
}

// Drawing --------------------------------------------------------------------

const FOCUSABLE = "button, input, select, textarea";

/** What a control is called, for the purpose of finding it again. */
function focusLabel(node) {
  return (node.textContent || "").trim() || node.getAttribute("aria-label") || "";
}

/**
 * Enough to find this control again in a tree that has not been built yet.
 *
 * A field is found by the key its value is already stored under. Everything
 * else — a button, a select — is found by what it says and by which of the
 * controls saying that it is, because `renderApp` builds a fresh tree every
 * draw and nothing in the old one survives to be compared against.
 *
 * This exists because the live feed redraws under whoever is at the table. It
 * already put the caret back for a half-typed quantity; without this, a player
 * who has tabbed to `Take 1` loses it every time somebody else grants an item.
 */
function focusMark(node) {
  if (!node || !app.contains(node) || !node.matches?.(FOCUSABLE)) return null;
  const key = node.dataset?.inputKey;
  if (key) {
    let caret = null;
    try {
      caret = [node.selectionStart, node.selectionEnd];
    } catch {
      // A number field refuses to report a selection, which is not a failure.
    }
    return { by: "input", key, caret };
  }
  const label = focusLabel(node);
  const peers = [...app.querySelectorAll(FOCUSABLE)].filter(
    (peer) => peer.tagName === node.tagName && focusLabel(peer) === label,
  );
  return { by: "label", tag: node.tagName, label, index: peers.indexOf(node) };
}

/** The same control in the tree that has just replaced the old one. */
function findMarked(mark) {
  if (!mark) return null;
  if (mark.by === "input") {
    return app.querySelector(`[data-input-key="${CSS.escape(mark.key)}"]`);
  }
  const peers = [...app.querySelectorAll(FOCUSABLE)].filter(
    (peer) => peer.tagName === mark.tag && focusLabel(peer) === mark.label,
  );
  return peers[mark.index] ?? null;
}

function draw() {
  const mark = focusMark(document.activeElement);
  const promptWasOpen = Boolean(app.querySelector("[data-prompt]"));

  app.replaceChildren(renderApp(state, handlers));

  // One control is in flight rather than the whole screen being unavailable.
  // Every control is still disabled — a second press is a second action with a
  // second key, which is the one thing the receipt cannot sort out — but only
  // the pressed one reads as working.
  if (state.busy && pressed) {
    const working = findMarked(pressed);
    if (working) {
      working.classList.add("press-pending");
      working.setAttribute("aria-busy", "true");
    }
  }

  const promptBox = app.querySelector("[data-prompt]");
  if (promptBox && !promptWasOpen) {
    // A question that appears where nobody is looking is a question nobody
    // answers. Focus goes to the first thing that answers it.
    const first = promptBox.querySelector("input, button");
    first?.focus();
    return;
  }

  const restored = findMarked(mark);
  if (!restored) return;
  restored.focus();
  try {
    if (mark.caret) restored.setSelectionRange(mark.caret[0], mark.caret[1]);
  } catch {
    // As above.
  }
}

/**
 * Say what happened, once.
 *
 * A refusal stays until it is read: it is a thing to do something about. A
 * success is the receipt for a press somebody just made and is already looking
 * at, so it goes away on its own rather than accumulating a Dismiss to press
 * for every take of the evening.
 */
const NOTICE_SECONDS = 8;

function notify(tone, text) {
  state.notice = { tone, text };
  clearTimeout(noticeTimer);
  noticeTimer = null;
  if (tone !== "bad") {
    noticeTimer = setTimeout(() => {
      noticeTimer = null;
      // Only if it is still the same notice: something newer has its own life.
      if (state.notice?.text === text) {
        state.notice = null;
        draw();
      }
    }, NOTICE_SECONDS * 1000);
  }
  draw();
}

/**
 * Put a question, and hold what answers it.
 *
 * Two kinds arrive: a quantity that moved under a press, raised by the action
 * layer, and an action that cannot be undone, raised before it is attempted.
 */
function ask({ text, confirmLabel, fields, run: answer }) {
  for (const field of fields || []) delete state.inputs[`prompt:${field.name}`];
  state.prompt = { text, confirmLabel, fields, run: answer };
  draw();
}

/**
 * One action at a time.
 *
 * A second press during the round trip would be a second action with a second
 * key, and the receipt cannot tell those apart — that is the one thing it is
 * not for.
 */
async function run(work) {
  if (state.busy) return;
  state.busy = true;
  state.notice = null;
  draw();
  try {
    await work();
  } catch (error) {
    console.error("Quartermaster could not complete that", error);
    notify("bad", error instanceof ApiError ? error.message : "That could not be completed.");
  } finally {
    state.busy = false;
    pressed = null;
    draw();
  }
}

/** The characters a sheet can be imported for. */
function activeRoster() {
  return state.roster.filter((character) => character.lifecycle === "ACTIVE");
}

/** A number somebody typed, or nothing, which is not the same as a zero. */
function typedOrNull(key) {
  const typed = Number.parseInt(state.inputs[key] ?? "", 10);
  return Number.isFinite(typed) ? typed : null;
}

/**
 * `name: value` per line, as an object.
 *
 * A line it cannot read is refused rather than skipped. Dropping it would
 * store a sheet missing whichever ability the DM typed a colon wrong on, and
 * a dossier is read as a complete reading of a character.
 */
function typedMap(key, values) {
  const result = {};
  for (const raw of (state.inputs[key] ?? "").split("\n")) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf(":");
    if (at < 1) throw new Error(`"${line}" is not a name and a value separated by a colon.`);
    const name = line.slice(0, at).trim();
    const value = line.slice(at + 1).trim();
    if (!name || !value) throw new Error(`"${line}" is missing a name or a value.`);
    if (values === "number") {
      const number = Number.parseInt(value, 10);
      if (!Number.isFinite(number)) throw new Error(`"${line}" needs a whole number.`);
      result[name] = number;
    } else {
      result[name] = value;
    }
  }
  return result;
}

/** A number somebody typed, or the default if they typed nothing usable. */
function typedNumber(key, fallback) {
  const typed = Number.parseInt(state.inputs[key] ?? "", 10);
  return Number.isFinite(typed) && typed > 0 ? typed : fallback;
}

/** How many item rows the Loot Drop form is showing. */
function dropRows() {
  return typedNumber("drop:rows", 1);
}

/** Empty one form, once what it described exists. */
function clearInputs(prefix) {
  for (const key of Object.keys(state.inputs)) {
    if (key.startsWith(prefix)) delete state.inputs[key];
  }
}

const actions = createActions({ notify, ask, reload: refresh });

const handlers = {
  select(screen) {
    state.screen = screen;
    state.prompt = null;
    // A health report or an export is a snapshot of the moment it was asked
    // for, so leaving one lying around a screen the DM came back to later is
    // the same mistake a stale panel was.
    state.report = null;
    draw();
    scheduleRefresh();
  },

  // Deliberately without a redraw: the field already shows what was typed, and
  // rebuilding it under the caret is how a live screen becomes unusable.
  setInput(key, value) {
    state.inputs[key] = value;
  },

  /**
   * The one typed value that does redraw.
   *
   * `setInput` deliberately does not, because rebuilding a form under the
   * caret is how a live screen becomes unusable. A filter is the exception
   * that proves it: the list underneath is the whole answer, so it has to
   * change as the letters arrive. `draw` puts the caret back, which is the
   * same mechanism that already lets the live feed redraw under somebody
   * halfway through typing a quantity.
   */
  setFilter(key, value) {
    state.inputs[key] = value;
    draw();
  },

  setDicePreset(expression, mode) {
    state.inputs["dice:expression"] = expression;
    state.inputs["dice:mode"] = mode;
    draw();
  },

  dismissNotice() {
    clearTimeout(noticeTimer);
    noticeTimer = null;
    state.notice = null;
    draw();
  },

  dismissPrompt() {
    state.prompt = null;
    draw();
  },

  confirmPrompt() {
    const prompt = state.prompt;
    if (!prompt) return;
    const values = {};
    for (const field of prompt.fields || []) {
      values[field.name] = state.inputs[`prompt:${field.name}`] ?? "";
    }
    state.prompt = null;
    run(() => prompt.run(values));
  },

  take: (item, amount) => run(() => actions.take(item, amount)),
  give: (item, destination, amount) => run(() => actions.give(item, destination, amount)),
  giveQuantity: (item, destination, quantity) =>
    run(() => actions.giveQuantity(item, destination, quantity)),
  claim: (dropItem, amount) => run(() => actions.claim(dropItem, amount)),

  use(item, quantity) {
    // The one operation with no way back, so it asks first and asks why. The
    // panel put the same two questions in a modal; the reason is what the
    // session log reads out, and it is optional there and here.
    ask({
      text: `Use ${quantity} ${item.item_name}? That takes it out of the campaign for good.`,
      confirmLabel: "Use it",
      fields: [{ name: "reason", placeholder: "Why? (optional)" }],
      run: (values) => actions.use(item, quantity, values.reason),
    });
  },

  returnCoin(amounts, destination) {
    if (Object.keys(amounts).length === 0) {
      notify("bad", "Name an amount to give.");
      return;
    }
    run(() => actions.returnCoin(amounts, destination));
  },

  giveCoin(character, amounts) {
    if (Object.keys(amounts).length === 0) {
      notify("bad", "Name an amount to give.");
      return;
    }
    run(() => actions.giveCoin(character, amounts));
  },

  // Stage 5 -------------------------------------------------------------------
  //
  // The DM's half. Two things recur. A form is validated here rather than at
  // the API, so an empty field is answered without a round trip — the API
  // refuses it too, and that refusal is the one that counts. And a form empties
  // itself only once what it described exists, because losing what was typed
  // to a refusal means typing it again.

  grant(itemName, quantity, provenance) {
    const name = (itemName || "").trim();
    if (!name) {
      notify("bad", "An item needs a name.");
      return;
    }
    run(async () => {
      if (await actions.grant(name, quantity, (provenance || "").trim())) clearInputs("grant:");
    });
  },

  correct(item) {
    // Removing from the Party Stash is the same call as a player using
    // something up, and it has the same absence of a way back — so it asks the
    // same two questions, and the reason is what the session log reads out.
    ask({
      text: `Remove how many ${item.item_name} from the Party Stash? ${item.quantity} are there, and what you remove leaves the campaign.`,
      confirmLabel: "Remove it",
      fields: [
        { name: "quantity", placeholder: "How many" },
        { name: "reason", placeholder: "Why? (optional)" },
      ],
      run: (values) => {
        const quantity = Number.parseInt(values.quantity, 10);
        if (!Number.isFinite(quantity) || quantity < 1) {
          notify("bad", "Name how many to remove.");
          return Promise.resolve();
        }
        return actions.correct(item, quantity, values.reason);
      },
    });
  },

  addDropRow() {
    // Bounded to what the API accepts in one drop, so the form cannot offer a
    // row that the request it builds would be refused for.
    state.inputs["drop:rows"] = String(Math.min(dropRows() + 1, DROP_ROW_LIMIT));
    draw();
  },

  createDrop(rows) {
    const items = [];
    for (let index = 0; index < rows; index += 1) {
      const name = (state.inputs[`drop:${index}:item`] || "").trim();
      // A blank row is a row the DM did not fill in, not an error. The form
      // offers more of them than most drops need on purpose.
      if (!name) continue;
      items.push({
        item_name: name,
        quantity: typedNumber(`drop:${index}:quantity`, 1),
        provenance: (state.inputs[`drop:${index}:provenance`] || "").trim() || null,
      });
    }
    if (items.length === 0) {
      notify("bad", "A Loot Drop needs at least one item.");
      return;
    }
    run(async () => {
      if (await actions.createDrop(items, typedNumber("drop:expiry", 72))) clearInputs("drop:");
    });
  },

  closeDrop: (dropId) => run(() => actions.closeDrop(dropId)),

  adjustTreasury(deltas, reason) {
    if (Object.keys(deltas).length === 0) {
      notify("bad", "Name an amount to add or take.");
      return;
    }
    run(() => actions.adjustTreasury(deltas, reason));
  },

  split(amounts) {
    if (Object.keys(amounts).length === 0) {
      notify("bad", "Name an amount to split.");
      return;
    }
    run(() => actions.split(amounts));
  },

  startSession: () => run(() => actions.startSession()),

  endSession(whereEnded, recordingUrl) {
    const where = (whereEnded || "").trim();
    if (!where) {
      // Required, and this is where it is worth saying why: it is the sentence
      // the next evening opens on.
      notify("bad", "Say where it ended. That is what the table picks up from next time.");
      return;
    }
    const recording = (recordingUrl || "").trim();
    // Answered here so a mistyped link does not cost the whole end-of-session
    // press. What a link has to be is the domain's answer, and it refuses this
    // too — this is the same refusal, one round trip earlier.
    if (recording && !/^https?:\/\/\S+$/i.test(recording)) {
      notify("bad", "A recording link has to start with http:// or https:// and hold no spaces.");
      return;
    }
    run(async () => {
      if (await actions.endSession(where, recording)) clearInputs("end:");
    });
  },

  openCombat() {
    // The one identifier this surface takes from Discord rather than from the
    // domain. It labels Quartermaster's own record of the fight and authorizes
    // nothing.
    const channelId = discordSdk?.channelId;
    if (!channelId) {
      notify("bad", "Discord has not said which channel this is, so combat cannot be recorded.");
      return;
    }
    run(() => actions.openCombat(String(channelId)));
  },

  closeCombat(outcome) {
    run(async () => {
      if (await actions.closeCombat((outcome || "").trim())) clearInputs("combat:");
    });
  },

  rollDice(expression, mode, label, visibility) {
    const value = (expression || "").trim();
    if (!value) {
      notify("bad", "Enter a dice expression such as d20 or 2d6+3.");
      return;
    }
    run(async () => {
      const result = await actions.rollDice(value, mode, (label || "").trim(), visibility);
      if (result) {
        state.dice.last = result;
        clearInputs("dice:");
        draw();
      }
    });
  },

  /**
   * Turn a typed sheet into the snapshot the API stores.
   *
   * Every value is passed through as given. Nothing here derives a modifier
   * from a score or a bonus from a level: the dossier explains a reading
   * somebody took, and a number Quartermaster worked out for itself would be
   * a rules engine wearing a snapshot's clothes.
   */
  importDossier() {
    const characterId = state.inputs["dossier:character"] || activeRoster()[0]?.id;
    if (!characterId) {
      notify("bad", "No active character is registered to import a sheet for.");
      return;
    }
    const character = state.roster.find((row) => row.id === characterId);
    const system = (state.inputs["dossier:system"] || "").trim();
    const rules = (state.inputs["dossier:rules"] || "").trim();
    if (!system || !rules) {
      notify("bad", "Name the system and the rules version the sheet is for.");
      return;
    }

    let maps;
    try {
      maps = {
        ability_scores: typedMap("dossier:scores", "number"),
        ability_modifiers: typedMap("dossier:modifiers", "number"),
        saving_throws: typedMap("dossier:saves", "number"),
        spell_resources: typedMap("dossier:resources", "number"),
        equipped: typedMap("dossier:equipped", "text"),
      };
    } catch (error) {
      notify("bad", error.message);
      return;
    }

    const snapshot = {
      character_id: characterId,
      system,
      rules_version: rules,
      source_reference: (state.inputs["dossier:reference"] || "").trim() || null,
      source_freshness: state.inputs["dossier:freshness"] || "CURRENT",
      level: typedOrNull("dossier:level"),
      proficiency_bonus: typedOrNull("dossier:proficiency"),
      armor_class: typedOrNull("dossier:ac"),
      hit_points: typedOrNull("dossier:hp"),
      temporary_hit_points: typedOrNull("dossier:temp") ?? 0,
      initiative: typedOrNull("dossier:initiative"),
      spell_attack_modifier: typedOrNull("dossier:spellattack"),
      spell_save_dc: typedOrNull("dossier:spelldc"),
      // When the reading was taken, which for a form is when it was typed in.
      // A DM copying from a sheet they read yesterday says so with STALE
      // rather than by backdating this.
      observed_at: new Date().toISOString(),
      ...maps,
    };

    run(async () => {
      if (await actions.importDossier(snapshot, character?.name || "that character")) {
        clearInputs("dossier:");
      }
    });
  },

  transition: (character, lifecycle) => run(() => actions.transition(character, lifecycle)),

  resolveEstate(character, destination) {
    const where =
      destination === "party"
        ? "the Party Stash"
        : state.roster.find((row) => row.id === destination)?.name || "them";
    ask({
      text: `Move everything ${character.name} was carrying to ${where}? Coin included.`,
      confirmLabel: "Move it",
      run: () => actions.resolveEstate(character, destination),
    });
  },

  runMaintenance: () => run(() => actions.runMaintenance()),
  backup: () => run(() => actions.backup()),

  // Neither of these changes anything, so neither goes through the action
  // layer: they are documents to read, and they are shown as documents.
  health() {
    run(async () => {
      state.report = (await api.health()).rendered;
    });
  },

  export() {
    run(async () => {
      state.report = (await api.exportRecord()).export;
    });
  },

  dismissReport() {
    state.report = null;
    draw();
  },

  /**
   * Take the export out of the frame.
   *
   * The runbook calls it the document to read during an outage, and during an
   * outage it is wanted anywhere but inside a Discord iframe. A download the
   * page starts itself is not reliable there; the clipboard is.
   */
  copyReport(text) {
    run(async () => {
      try {
        await navigator.clipboard.writeText(text);
        notify("ok", "The export is on your clipboard.");
      } catch {
        // A clipboard write needs a permission the embed may not have, and
        // failing silently would leave somebody pressing a button that does
        // nothing during the one hour they need it most.
        notify(
          "bad",
          "Discord would not let the page write to the clipboard. Select the text and copy it.",
        );
      }
    });
  },

  register(discordUserId, personName) {
    ask({
      text: `Register a character for ${personName}.`,
      confirmLabel: "Register",
      fields: [{ name: "name", placeholder: "Character name" }],
      run: (values) => {
        const name = (values.name || "").trim();
        if (!name) {
          notify("bad", "A character needs a name.");
          return Promise.resolve();
        }
        return actions.registerCharacter(name, String(discordUserId));
      },
    });
  },
};

// Boot -----------------------------------------------------------------------

async function authenticate(sdk) {
  // The authorization code proves who the player is; the client cannot mint
  // one. Everything after this is the server's answer about them, never the
  // client's claim.
  const { code } = await sdk.commands.authorize({
    client_id: CLIENT_ID,
    response_type: "code",
    state: "",
    prompt: "none",
    scope: ["identify", "guilds.members.read"],
  });

  const session = await api.exchangeCode(code, sdk.instanceId);
  setSessionToken(session.token);

  // Discord's own token, handed straight to the SDK. Without this the RPC
  // channel will not answer for the participant roster.
  await sdk.commands.authenticate({ access_token: session.discord_access_token });

  return { id: session.actor_id, isDm: session.is_dm };
}

// Keyboard and pressing ------------------------------------------------------
//
// All of it is delegated from the root rather than bound per control, because
// `renderApp` replaces the whole tree on every draw and a listener attached to
// a button is gone the next time anyone else at the table takes something.

/**
 * Which control is working.
 *
 * Captured on the way down, before the handler runs and the redraw it causes
 * throws the pressed element away.
 */
app.addEventListener(
  "click",
  (event) => {
    const control = event.target.closest?.("button.press");
    if (control) pressed = focusMark(control);
  },
  true,
);

/**
 * What Enter means in a form that is not a form.
 *
 * Every screen here is built out of divs, so the browser's own "Enter submits"
 * never applied. A DM granting ten items after a fight was reaching for the
 * mouse between every one of them.
 *
 * Enter presses the primary control of whatever block the field is in — the
 * same one the eye reads as the point of that block. A filter is exempt: its
 * list is already answering as the letters arrive, so there is nothing for
 * Enter to do that has not happened.
 */
const SUBMIT_SCOPE = ".prompt, .form, .dm-block, tr";

function submitControl(scope) {
  return (
    scope.querySelector("button.press.primary:not(:disabled)") ||
    scope.querySelector("button.press:not(.quiet):not(:disabled)")
  );
}

app.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || event.shiftKey) return;
  const field = event.target;
  if (!field.matches?.("input") || field.type === "search") return;
  // Outwards until a block is found that has something to press. A row of the
  // Loot Drop form has three fields and no control of its own; the control
  // belongs to the form the row is in, which is the next scope up.
  let scope = field.closest(SUBMIT_SCOPE);
  while (scope) {
    const primary = submitControl(scope);
    if (primary) {
      event.preventDefault();
      primary.click();
      return;
    }
    scope = scope.parentElement?.closest(SUBMIT_SCOPE) ?? null;
  }
});

/**
 * Escape answers the question with "no".
 *
 * The prompt gates the one action with no way back, and a dialog that can only
 * be dismissed by finding its Cancel button is one people answer "yes" to by
 * accident.
 */
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !state.prompt) return;
  event.preventDefault();
  handlers.dismissPrompt();
});

/**
 * Hold focus inside the question while it is being asked.
 *
 * Without this, Tab walks out of the dialog and into the screen behind it,
 * where every control is disabled and none of them is the answer.
 */
document.addEventListener("keydown", (event) => {
  if (event.key !== "Tab") return;
  const box = app.querySelector("[data-prompt]");
  if (!box) return;
  const stops = [...box.querySelectorAll(FOCUSABLE)].filter((node) => !node.disabled);
  if (stops.length === 0) return;
  const first = stops[0];
  const last = stops[stops.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  } else if (!box.contains(document.activeElement)) {
    event.preventDefault();
    first.focus();
  }
});

async function watchParticipants(sdk) {
  const apply = ({ participants }) => {
    state.participants = participants || [];
    draw();
  };
  const initial = await sdk.commands.getInstanceConnectedParticipants();
  apply(initial);
  // The party joining and leaving is Discord's fact, not ours. Nothing here
  // builds a lobby, tracks presence, or reconciles a roster.
  sdk.subscribe("ACTIVITY_INSTANCE_PARTICIPANTS_UPDATE", apply);
}

async function boot() {
  if (!CLIENT_ID) {
    app.replaceChildren(
      renderError(
        "VITE_DISCORD_CLIENT_ID is not set, so the Activity cannot identify itself to Discord.",
      ),
    );
    return;
  }

  const sdk = new DiscordSDK(CLIENT_ID);
  discordSdk = sdk;
  try {
    app.replaceChildren(renderStatus("Waiting for Discord…"));
    await sdk.ready();

    app.replaceChildren(renderStatus("Checking who you are…"));
    state.actor = await authenticate(sdk);
    // One renewal path for the reads and the socket both. It re-runs the
    // handshake rather than refreshing anything, because the authorization
    // code is the only thing that proves who is asking.
    onExpiry(async () => {
      state.actor = await authenticate(sdk);
    });

    app.replaceChildren(renderStatus("Reading the Party Stash…"));
    // The socket opens before the first read, and deliberately: a change that
    // lands between the read and the connection would otherwise fall in the
    // gap between them. Connecting first can only cost a redundant refresh.
    openLiveFeed({
      token: sessionTokenValue,
      renew: () =>
        authenticate(sdk).then((actor) => {
          state.actor = actor;
        }),
      onChange: scheduleRefresh,
      onStatus: (status) => {
        state.live = status;
        draw();
      },
    });

    await refresh();
    await watchParticipants(sdk);
  } catch (error) {
    // A failure here is a handshake, proxy, or hosting failure — exactly what
    // this stage exists to surface — so it says which, rather than leaving a
    // blank frame.
    const detail =
      error instanceof ApiError
        ? `The API answered ${error.status}: ${error.message}`
        : error?.message || String(error);
    app.replaceChildren(renderError(detail));
  }
}

boot();
