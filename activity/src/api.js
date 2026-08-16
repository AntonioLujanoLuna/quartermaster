// The client's half of the trust boundary.
//
// The session token is held here and sent on every call. It is not the Discord
// access token — that one goes to the SDK and nowhere else. Nothing in this
// module sends an actor id, because the API would ignore it: the server reads
// the actor out of the token it signed.

const BASE = "/.proxy/api";

let sessionToken = null;
let renew = null;

export class ApiError extends Error {
  constructor(status, detail, code) {
    super(detail || `request failed with ${status}`);
    this.status = status;
    // What the caller is expected to do about it, rather than what to say:
    // STALE is a question for the player, HANDLE means prepare the action
    // again, REFUSED is the domain's answer. Absent on 401 and 403, which are
    // about the token rather than about the campaign.
    this.code = code || null;
  }
}

/**
 * A key for one action a player took, generated when they take it.
 *
 * The receipt behind every mutation keys on this, so a retry of the same
 * request returns the answer already stored rather than acting twice. Which
 * means the key has to be minted where the action starts and carried through
 * every retry of it — a key per request would make a retry a second take.
 */
export function actionKey() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

export function setSessionToken(token) {
  sessionToken = token;
}

export function sessionTokenValue() {
  return sessionToken;
}

/**
 * How to get another session token when this one stops being accepted.
 *
 * A token lasts an hour and a session at the table does not, so without this
 * every screen in the party goes dead partway through the evening — and with a
 * live feed on it, it goes dead while still looking connected.
 */
export function onExpiry(handler) {
  renew = handler;
}

async function request(path, options = {}, { retried = false } = {}) {
  const headers = { ...(options.headers || {}) };
  if (sessionToken) headers.Authorization = `Bearer ${sessionToken}`;
  if (options.body) headers["Content-Type"] = "application/json";

  const response = await fetch(`${BASE}${path}`, { ...options, headers });
  if (response.status === 401 && renew && !retried) {
    // Once. A second refusal after a fresh token is a real refusal, and
    // retrying it again would be a loop rather than a repair.
    await renew();
    return request(path, options, { retried: true });
  }
  if (!response.ok) {
    let body = null;
    try {
      body = await response.json();
    } catch {
      // A proxy or a crash can answer with something that is not JSON, and the
      // status is still the useful part.
    }
    throw new ApiError(response.status, body?.detail, body?.code);
  }
  return response.json();
}

/**
 * A mutation: a body, and the key for the action it belongs to.
 *
 * The key is a parameter rather than something generated here, because one
 * action can be more than one request — a take that comes back stale is
 * answered on a different route — and each of those is its own action to the
 * receipts table.
 */
function mutate(path, body, key) {
  return request(path, {
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": key },
  });
}

export const api = {
  exchangeCode: (code, instanceId) =>
    request("/token", {
      method: "POST",
      body: JSON.stringify({ code, instance_id: instanceId }),
    }),

  // Reads
  home: () => request("/home"),
  stash: () => request("/stash"),
  myItems: () => request("/me/items"),
  loot: () => request("/loot"),
  treasury: () => request("/treasury"),
  characters: () => request("/characters"),

  // Preparing an action mints the handle it will spend, and is not itself an
  // action: no key, and a retry costs an unspent handle rather than a receipt.
  prepareTake: (stackId, amount) =>
    request("/stash/take/prepare", {
      method: "POST",
      body: JSON.stringify({ stack_id: stackId, amount }),
    }),
  prepareGive: (stackId) =>
    request("/items/give/prepare", { method: "POST", body: JSON.stringify({ stack_id: stackId }) }),

  // Mutations
  take: (handleId, key) => mutate("/stash/take", { handle_id: handleId }, key),
  confirmTake: (handleId, key) => mutate("/stash/take/confirm", { handle_id: handleId }, key),
  give: (handleId, destination, key) =>
    mutate("/items/give", { handle_id: handleId, destination }, key),
  confirmGive: (handleId, destination, key) =>
    mutate("/items/give/confirm", { handle_id: handleId, destination }, key),
  giveSome: (itemName, quantity, destination, key) =>
    mutate("/items/give/some", { item_name: itemName, quantity, destination }, key),
  use: (stackId, quantity, reason, key) =>
    mutate("/items/use", { stack_id: stackId, quantity, reason }, key),
  claim: (dropItemId, amount, key) =>
    mutate("/loot/claim", { drop_item_id: dropItemId, amount }, key),
  returnCoin: (amounts, destination, key) =>
    mutate("/treasury/return", { amounts, destination }, key),
  giveCoin: (characterId, amounts, key) =>
    mutate("/treasury/give", { character_id: characterId, amounts }, key),
  registerCharacter: (name, discordUserId, key) =>
    mutate("/characters", { name, discord_user_id: discordUserId }, key),
};
