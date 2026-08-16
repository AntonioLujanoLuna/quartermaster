// Stage 2 of docs/activity-migration-plan.md: the walking skeleton.
//
// One read-only screen — the Party Stash — and the party roster beside it. The
// point of this stage is not the screen. It is proving that the handshake, the
// proxy, and the hosting all work, because those are the parts that can only
// be discovered against real Discord, and the domain has not been touched to
// find out.

import { DiscordSDK } from "@discord/embedded-app-sdk";
import { api, ApiError, setSessionToken } from "./api.js";
import { renderStash, renderError, renderStatus } from "./render.js";

const CLIENT_ID = import.meta.env.VITE_DISCORD_CLIENT_ID;

const app = document.getElementById("app");
const state = {
  actor: null,
  participants: [],
  stash: null,
  home: null,
};

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

    app.replaceChildren(renderStatus("Reading the Party Stash…"));
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
