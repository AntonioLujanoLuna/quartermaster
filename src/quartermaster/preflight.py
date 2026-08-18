"""Stage 0 of `docs/activity-migration-plan.md`, as far as a machine can carry it.

Stage 0 is hosting, and its exit criterion is that an origin exists and serves
the page through Discord's proxy. Three of the things that has to be true are
facts about Discord — a tunnel is up, a URL mapping points at it, the app is
installed in the guild — and no amount of code establishes them. Everything
else is a fact about this machine, and every one of those is a way for the
first launch to fail with a blank frame inside a Discord client, which is the
worst place to debug any of them.

So this serves the real application on the configured bind and asks it the
questions the first launch will ask:

- is the Activity configured at all, and can the live feed actually be served;
- was the page built, and built with a client id — a bundle built without one
  compiles to nothing, and looks exactly like a bundle that works;
- does the origin answer on both path forms the proxy may use;
- does the page find its own assets;
- is the trust boundary live — does an unauthenticated read get refused;
- does `/api/live` accept a WebSocket and refuse an unauthenticated one.

What it deliberately does not do is touch the campaign. It runs against a
throwaway database, because nothing it checks is a fact about the table's
items, and because the architecture note is serious: one process, one SQLite
writer. A check that opened the live database next to a running bot would be
the second writer that note exists to forbid.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .api_app import PROXY_PREFIX, UNAUTHORIZED, create_app
from .api_auth import DiscordIdentityProvider
from .config import Settings
from .db import SQLiteStore
from .handles import HandleRepository
from .receipts import ReceiptRepository

__all__ = ["Check", "run_preflight"]

#: How long to wait for the server to come up before calling it a failure.
STARTUP_TIMEOUT_SECONDS = 20.0

#: How long any single request may take. Everything here is loopback.
REQUEST_TIMEOUT_SECONDS = 10.0

#: A string the built bundle must contain.
#:
#: Vite replaces `import.meta.env` at build time, so a build with no
#: `VITE_DISCORD_CLIENT_ID` makes the client id statically undefined, `boot()`
#: returns on its first branch, and the bundler removes the whole application
#: as unreachable. The result is a successful build of nothing, and it is
#: indistinguishable from a working one by size alone. This is the path every
#: call in `api.js` is addressed to, so a bundle that still contains the
#: application still contains it.
BUNDLE_EVIDENCE = "/.proxy/api"



@dataclass(frozen=True)
class Check:
    """One question, its answer, and what to do when the answer is no."""

    name: str
    passed: bool
    detail: str
    remedy: str = ""

    def render(self) -> str:
        marker = "PASS" if self.passed else "FAIL"
        line = f"[{marker}] {self.name}: {self.detail}"
        if not self.passed and self.remedy:
            line += f"\n         {self.remedy}"
        return line


def _websocket_support() -> Check:
    import importlib.util

    for module in ("websockets", "wsproto"):
        if importlib.util.find_spec(module) is not None:
            return Check("live feed", True, f"uvicorn can serve WebSockets with {module}")
    return Check(
        "live feed",
        False,
        "neither websockets nor wsproto is installed",
        "uv sync --extra activity — without it the screen loads, says it is connecting, and never moves",
    )


def _configuration(settings: Settings) -> list[Check]:
    checks = [
        Check(
            "configuration",
            settings.activity_enabled,
            "client id and secret are set" if settings.activity_enabled else "the Activity is not enabled",
            "set QM_DISCORD_CLIENT_ID and QM_DISCORD_CLIENT_SECRET; the bot runs without them and the Activity does not",
        )
    ]
    if settings.activity_origin:
        checks.append(
            Check(
                "split origin",
                True,
                f"CORS will be opened for {settings.activity_origin}",
                "",
            )
        )
    return checks


def _built_page(settings: Settings) -> list[Check]:
    distribution = settings.activity_dist
    if distribution is None:
        return [
            Check(
                "built page",
                False,
                "QM_ACTIVITY_DIST is not set, so the API serves no page",
                "build it (cd activity; npm run build) and set QM_ACTIVITY_DIST=activity/dist — "
                "serving the page from the API's own origin is what makes one URL mapping enough",
            )
        ]
    distribution = distribution.expanduser()
    index = distribution / "index.html"
    if not index.is_file():
        return [
            Check(
                "built page",
                False,
                f"no index.html under {distribution}",
                "cd activity; npm install; npm run build",
            )
        ]

    checks = [Check("built page", True, f"index.html is present under {distribution}")]
    bundles = sorted((distribution / "assets").glob("*.js")) if (distribution / "assets").is_dir() else []
    if not bundles:
        checks.append(
            Check(
                "bundle",
                False,
                "the build produced no JavaScript",
                "cd activity; npm run build",
            )
        )
        return checks

    built_with_client_id = any(BUNDLE_EVIDENCE in bundle.read_text(encoding="utf-8", errors="replace") for bundle in bundles)
    checks.append(
        Check(
            "bundle",
            built_with_client_id,
            (
                f"{bundles[0].name} carries the application"
                if built_with_client_id
                else "the bundle compiled to nothing"
            ),
            "rebuild with VITE_DISCORD_CLIENT_ID set — without it Vite tree-shakes the whole "
            "application away and the build still succeeds",
        )
    )
    return checks


def _get(url: str) -> tuple[int, bytes, str]:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers.get("Content-Type", "")


@contextmanager
def _served(settings: Settings, bind: str) -> Iterator[str]:
    """The real application, on a real socket, for the length of the checks."""
    import uvicorn

    from .discord_adapter import assemble_services, context_for

    host, _, port = bind.rpartition(":")
    with tempfile.TemporaryDirectory() as directory:
        # A throwaway database on purpose: see the module docstring. Nothing
        # below asks a question about the campaign.
        store = SQLiteStore(Path(directory) / "preflight.sqlite").open()
        try:
            receipts = ReceiptRepository(store)
            services = assemble_services(store, receipts, HandleRepository(store))
            context = context_for(settings, services)
            client_id, client_secret = settings.require_activity()
            app = create_app(
                context,
                DiscordIdentityProvider(
                    client_id=client_id, client_secret=client_secret, guild_id=settings.guild_id
                ),
            )
            server = uvicorn.Server(uvicorn.Config(app, host=host, port=int(port), log_level="warning"))
            thread = threading.Thread(target=server.run, name="preflight-api", daemon=True)
            thread.start()
            try:
                _await_startup(server)
                yield f"http://{host}:{port}"
            finally:
                server.should_exit = True
                thread.join(timeout=STARTUP_TIMEOUT_SECONDS)
        finally:
            store.close()


def _await_startup(server: object) -> None:
    import time

    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if getattr(server, "started", False):
            return
        time.sleep(0.05)
    raise TimeoutError("the API did not finish starting")


def _serving_checks(base: str, settings: Settings) -> list[Check]:
    checks: list[Check] = []

    status, body, _ = _get(f"{base}/api/health")
    healthy = status == 200
    schema = ""
    if healthy:
        try:
            schema = f" at schema {json.loads(body)['schema_version']}"
        except (ValueError, KeyError):
            healthy = False
    checks.append(
        Check(
            "origin",
            healthy,
            f"the API answers on {base}{schema}" if healthy else f"/api/health answered {status}",
            "check QM_API_BIND, and that nothing else holds the port",
        )
    )

    proxied, _, _ = _get(f"{base}{PROXY_PREFIX}/api/health")
    checks.append(
        Check(
            "proxy prefix",
            proxied == 200,
            (
                f"{PROXY_PREFIX}/api answers as well as /api"
                if proxied == 200
                else f"{PROXY_PREFIX}/api/health answered {proxied}"
            ),
            "the client addresses every call under this prefix; both forms have to answer",
        )
    )

    unauthorized, _, _ = _get(f"{base}/api/stash")
    checks.append(
        Check(
            "trust boundary",
            unauthorized == 401,
            (
                "an unauthenticated read is refused"
                if unauthorized == 401
                else f"an unauthenticated read answered {unauthorized}, not 401"
            ),
            "every read but /api/health requires a session token this process signed",
        )
    )

    if settings.activity_dist is not None:
        checks.extend(_page_checks(base))
    return checks


def _page_checks(base: str) -> list[Check]:
    checks: list[Check] = []
    status, body, content_type = _get(f"{base}/")
    page = body.decode("utf-8", errors="replace")
    served = status == 200 and "text/html" in content_type
    checks.append(
        Check(
            "page",
            served,
            "the built page is served at /" if served else f"/ answered {status} ({content_type})",
            "set QM_ACTIVITY_DIST to the built directory",
        )
    )
    proxied_status, _, _ = _get(f"{base}{PROXY_PREFIX}/")
    checks.append(
        Check(
            "page behind the prefix",
            proxied_status == 200,
            (
                f"the page is served at {PROXY_PREFIX}/ too"
                if proxied_status == 200
                else f"{PROXY_PREFIX}/ answered {proxied_status}"
            ),
            "",
        )
    )
    if not served:
        return checks

    # The page names its own assets; asking for one is what proves a mapping of
    # `/` reaches more than index.html.
    for marker in ('src="', 'href="'):
        for fragment in page.split(marker)[1:]:
            asset = fragment.split('"')[0]
            if not asset.startswith("/assets/"):
                continue
            asset_status, _, _ = _get(f"{base}{asset}")
            checks.append(
                Check(
                    "assets",
                    asset_status == 200,
                    f"{asset} is served" if asset_status == 200 else f"{asset} answered {asset_status}",
                    "",
                )
            )
            break
    return checks


def _socket_check(base: str) -> Check:
    """Prove the upgrade is really served, and that the feed checks the token.

    Without a WebSocket implementation uvicorn answers this with a 404, which
    reads as a missing route. With one, the socket opens and the first frame
    has to carry a session token — so a deliberately bad one is answered with
    close code 4401, and that single exchange proves both the live feed and the
    trust boundary on it.

    Presenting a bad token rather than none, because presenting none is
    answered by the server's own handshake timeout: the check would wait out
    `HANDSHAKE_SECONDS` and report a timeout that means the opposite of a
    fault.
    """
    try:
        from websockets.sync.client import connect
    except ImportError:
        return Check("socket", False, "no WebSocket client available to check with", "uv sync --extra activity")

    url = base.replace("http://", "ws://") + "/api/live"
    try:
        with connect(url, open_timeout=REQUEST_TIMEOUT_SECONDS) as socket_:
            socket_.send(json.dumps({"token": "preflight-is-not-a-token"}))
            socket_.recv(timeout=REQUEST_TIMEOUT_SECONDS)
    except Exception as error:  # noqa: BLE001 - every failure here is reported, not raised
        if getattr(error, "code", None) == UNAUTHORIZED:
            return Check("socket", True, "/api/live upgrades, and refuses a token it did not sign")
        return Check(
            "socket",
            False,
            f"/api/live did not upgrade: {error}",
            "a 404 here is a missing WebSocket implementation, not a missing route",
        )
    return Check(
        "socket",
        False,
        "/api/live answered a forged token instead of closing",
        "the feed must refuse a socket whose token this process did not sign",
    )


def _port_is_free(bind: str) -> Check:
    host, _, port = bind.rpartition(":")
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(1.0)
    try:
        if probe.connect_ex((host or "127.0.0.1", int(port))) == 0:
            return Check(
                "bind",
                False,
                f"something is already listening on {bind}",
                "if that is the bot, it is already serving the Activity and there is nothing here to add; "
                "otherwise stop it, or pass --bind to check this build on another port",
            )
    finally:
        probe.close()
    return Check("bind", True, f"{bind} is free to serve on")


def run_preflight(settings: Settings, *, bind: str | None = None) -> tuple[list[Check], str]:
    """Every Stage 0 question this machine can answer, and what is left over."""
    address = bind or settings.api_bind
    checks = _configuration(settings)
    checks.append(_websocket_support())
    checks.extend(_built_page(settings))

    if not settings.activity_enabled:
        # Nothing below can be asked: the app cannot be built without a secret
        # to sign session tokens with.
        return checks, _remaining(settings, address)

    port_check = _port_is_free(address)
    checks.append(port_check)
    if port_check.passed:
        try:
            with _served(settings, address) as base:
                checks.extend(_serving_checks(base, settings))
                checks.append(_socket_check(base))
        except Exception as error:  # noqa: BLE001 - a failure to serve is a result, not a crash
            checks.append(
                Check("origin", False, f"the API could not be served on {address}: {error}", "")
            )
    return checks, _remaining(settings, address)


def _remaining(settings: Settings, bind: str) -> str:
    """The half of Stage 0 that is a fact about Discord rather than this machine."""
    return (
        "Still to do by hand, in this order:\n"
        f"  1. Open a tunnel to {bind} and note the https hostname it prints.\n"
        "     cloudflared tunnel --url http://" + bind + "   (throwaway, new hostname each restart)\n"
        "     tailscale funnel --bg " + bind.rpartition(":")[2] + "        (stable hostname, free)\n"
        "  2. Developer Portal -> Activities -> Settings: enable Activities.\n"
        "  3. Developer Portal -> Activities -> URL Mappings: map the root prefix / to that\n"
        "     hostname, without the scheme. One mapping is enough — the page and the API\n"
        "     share this origin.\n"
        "  4. Confirm the tunnel from another machine:\n"
        "     curl https://<hostname>/api/health\n"
        "  5. Launch the Activity from a voice channel in guild " + settings.guild_id + ".\n"
        "See docs/runbook.md#serving-the-activity-without-paying-for-hosting."
    )


def render_preflight(checks: list[Check], remaining: str) -> str:
    failed = [check for check in checks if not check.passed]
    lines = [check.render() for check in checks]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} of {len(checks)} checks failed. The origin is not ready to map.")
    else:
        lines.append(f"All {len(checks)} checks passed. This machine's half of Stage 0 is done.")
        lines.append("")
        lines.append(remaining)
    return "\n".join(lines)
