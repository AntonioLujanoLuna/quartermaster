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
  constructor(status, detail) {
    super(detail || `request failed with ${status}`);
    this.status = status;
  }
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
    let detail = null;
    try {
      detail = (await response.json()).detail;
    } catch {
      // A proxy or a crash can answer with something that is not JSON, and the
      // status is still the useful part.
    }
    throw new ApiError(response.status, detail);
  }
  return response.json();
}

export const api = {
  exchangeCode: (code, instanceId) =>
    request("/token", {
      method: "POST",
      body: JSON.stringify({ code, instance_id: instanceId }),
    }),
  home: () => request("/home"),
  stash: () => request("/stash"),
  myItems: () => request("/me/items"),
  treasury: () => request("/treasury"),
};
