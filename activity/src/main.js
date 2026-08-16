// Stages 2 and 3 of docs/activity-migration-plan.md: the walking skeleton, live.
//
// One read-only screen — the Party Stash — the party roster beside it, and a
// socket that says when to read the screen again. The point of Stage 2 was
// proving the handshake, the proxy, and the hosting. The point of Stage 3 is
// that the table is looking at one thing rather than six copies of one thing,
// each as stale as the last time its owner pressed a button.

import { DiscordSDK } from "@discord/embedded-app-sdk";
import { api, ApiError, onExpiry, setSessionToken, sessionTokenValue } from "./api.js";
import { openLiveFeed } from "./live.js";
import { renderStash, renderError, renderStatus } from "./render.js";

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;

// Long enough to collapse the burst a single action produces — a grant is one
// event, but a session end is many — and short enough that nobody at the table
// perceives it as a delay.
const REFRESH_DEBOUNCE_MS = 150;

const app = document.getElementById("app");
const state = {
  actor: null,
  participants: [],
  stash: null,
  home: null,
  live: "connecting",
};

let refreshTimer = null;
let refreshing = null;

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

async function refresh() {
  const [home, stash] = await Promise.all([api.home(), api.stash()]);
  state.home = home;
  state.stash = stash;
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

function draw() {
  app.replaceChildren(renderStash(state));
}

async function boot() {
  if (!CLIENT_ID) {
    app.replaceChildren(
      renderError("VITE_DISCORD_CLIENT_ID is not set, so the Activity cannot identify itself to Discord."),
    );
    return;
  }

  const sdk = new DiscordSDK(CLIENT_ID);
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
