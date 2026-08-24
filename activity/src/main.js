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
    refreshing = (refreshing || Promise.resolve())
      .then(refresh)
      .catch((error) => {
        // A failed refresh is not a dead screen. The socket is still open, so
        // the next change is another chance, and what is rendered is still the
        // last state that was actually read.
        console.warn("Quartermaster could not refresh", error);
      });
  }, REFRESH_DEBOUNCE_MS);
}

// Drawing --------------------------------------------------------------------

function draw() {
  // A live screen redraws while somebody is halfway through typing a quantity
  // into it. The value survives in `state.inputs`; this is what stops the
  // caret from moving to the top of the page with it.
  const focused = document.activeElement;
  const key = focused?.dataset?.inputKey;
  let caret = null;
  try {
    caret = key ? [focused.selectionStart, focused.selectionEnd] : null;
  } catch {
    // A number field refuses to report a selection, which is not a failure.
  }

  app.replaceChildren(renderApp(state, handlers));

  if (!key) return;
  const restored = app.querySelector(`[data-input-key="${CSS.escape(key)}"]`);
  if (!restored) return;
  restored.focus();
  try {
    if (caret) restored.setSelectionRange(caret[0], caret[1]);
  } catch {
    // As above.
  }
}

function notify(tone, text) {
  state.notice = { tone, text };
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
    draw();
  }
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

  setDicePreset(expression, mode) {
    state.inputs["dice:expression"] = expression;
    state.inputs["dice:mode"] = mode;
    draw();
  },

  dismissNotice() {
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

  endSession(whereEnded) {
    const where = (whereEnded || "").trim();
    if (!where) {
      // Required, and this is where it is worth saying why: it is the sentence
      // the next evening opens on.
      notify("bad", "Say where it ended. That is what the table picks up from next time.");
      return;
    }
    run(async () => {
      if (await actions.endSession(where)) clearInputs("end:");
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
      renderError("VITE_DISCORD_CLIENT_ID is not set, so the Activity cannot identify itself to Discord."),
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
      renew: () => authenticate(sdk).then((actor) => {
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
