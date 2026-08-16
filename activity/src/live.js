// The client's half of the live feed.
//
// Stage 3 of docs/activity-migration-plan.md. The socket carries change
// notifications, not state: it says something happened and how far the ledger
// has moved, and this module answers by asking for the reads on screen again.
// Rendering from the socket would make it a second source of truth for facts
// the reads already answer for.
//
// The cursor is what makes a dropped connection a gap to fill rather than a
// screen to rebuild. Every notice carries the sequence it reached; reconnecting
// asks to resume from the last one seen, and the server either replays the gap
// or says it is too wide and to read everything again.

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 30000;

// Private-range close codes the API uses. 4401 is the one worth telling apart:
// a session token lasts an hour and an evening does not, so an expired token is
// a thing to fix and reconnect from rather than a reason to stop.
const UNAUTHORIZED = 4401;

function socketUrl(since) {
  const url = new URL("/.proxy/api/live", window.location.href);
  url.protocol = url.protocol === "http:" ? "ws:" : "wss:";
  if (since !== null && since !== undefined) url.searchParams.set("since", String(since));
  return url.toString();
}

/**
 * Hold a live connection open, reconnecting for as long as nobody closes it.
 *
 * @param token      a function returning the current session token
 * @param renew      called when the server refuses the token; resolves to a new one
 * @param onChange   called when something changed and the screen should read again
 * @param onStatus   called with "live", "connecting", or "offline"
 */
export function openLiveFeed({ token, renew, onChange, onStatus }) {
  let socket = null;
  let sequence = null;
  let attempt = 0;
  let closed = false;
  let timer = null;

  const status = (state) => onStatus?.(state);

  function scheduleReconnect() {
    if (closed || timer) return;
    // Backing off rather than retrying tightly: a server that is down stays
    // down for a while, and six clients at one table hammering it does not
    // help any of them.
    const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_MIN_MS * 2 ** attempt);
    attempt += 1;
    timer = setTimeout(() => {
      timer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (closed) return;
    status("connecting");
    let opened;
    try {
      opened = new WebSocket(socketUrl(sequence));
    } catch {
      scheduleReconnect();
      return;
    }
    socket = opened;

    opened.addEventListener("open", () => {
      // The token goes in the first frame rather than the URL: a query string
      // is a bearer credential in every log between here and the player.
      opened.send(JSON.stringify({ token: token() }));
    });

    opened.addEventListener("message", (event) => {
      let notice;
      try {
        notice = JSON.parse(event.data);
      } catch {
        return;
      }
      if (notice.type === "heartbeat") return;
      if (typeof notice.sequence === "number") sequence = notice.sequence;
      if (notice.type === "hello") {
        attempt = 0;
        status("live");
        return;
      }
      // Both "events" and "reset" mean the same thing to a screen this size:
      // what is rendered may be out of date, so read it again. They diverge
      // when there is more than one screen and only some of them care.
      if (notice.type === "events" || notice.type === "reset") onChange?.(notice);
    });

    opened.addEventListener("close", async (event) => {
      socket = null;
      if (closed) return;
      status("offline");
      if (event.code === UNAUTHORIZED && renew) {
        // The token this connection presented is no longer good. Getting
        // another one is the whole repair, and it is also what the reads need,
        // so it happens once here rather than once per read.
        try {
          await renew();
          attempt = 0;
        } catch {
          // Nothing else to try; the backoff below is the remaining answer.
        }
      }
      scheduleReconnect();
    });

    // A socket error is always followed by a close event, which is where the
    // reconnect lives. This exists so the error is not unhandled.
    opened.addEventListener("error", () => {});
  }

  connect();

  return {
    get sequence() {
      return sequence;
    },
    close() {
      closed = true;
      if (timer) clearTimeout(timer);
      socket?.close();
    },
  };
}
