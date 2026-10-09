#!/usr/bin/env python3
"""PhonePad: use an iPhone (Safari) as a game controller for the Mac.

The phone page talks to this server over a WebSocket; the server turns stick
and button messages into synthetic keyboard/mouse events for the frontmost app.
"""
import argparse
import asyncio
import hmac
import ipaddress
import json
import plistlib
import uuid
import math
import os
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

from aiohttp import WSMsgType, web

import certs
import macinput

HERE = Path(__file__).resolve().parent
TOKEN_FILE = HERE / ".token"
INDEX_FILE = HERE / "static" / "index.html"
LOG_FILE = HERE / "phonepad.log"
CERT_DIR = HERE / ".certs"

STATE = web.AppKey("state", dict)
TICK_HZ = 120
STALE_AFTER = 1.5          # seconds without any message before everything is released
MAX_MSG_CHARS = 1024
MAX_LOOK_STEP = 400.0      # phone px per look message; anything bigger is clamped
MIN_PRESS = 0.06           # every key stays down at least this long, so games see quick taps


def log(msg, console=True):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    if console:
        print(line, flush=True)
    try:
        with LOG_FILE.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_token(regenerate=False):
    if not regenerate and TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if len(token) >= 16:
            return token
    token = secrets.token_urlsafe(16)
    TOKEN_FILE.write_text(token + "\n")
    os.chmod(TOKEN_FILE, 0o600)
    return token


def number_pair(v):
    """[x, y] of finite, non-bool numbers -> (x, y) floats, or None."""
    if not isinstance(v, list) or len(v) != 2:
        return None
    if not all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in v):
        return None
    try:
        x, y = float(v[0]), float(v[1])
    except OverflowError:
        return None
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x, y)


def unit_vec(v):
    """[x, y] -> (x, y) clamped to the unit circle, or None if malformed."""
    pair = number_pair(v)
    if pair is None:
        return None
    x, y = pair
    mag = math.hypot(x, y)
    if mag > 1.0:
        x, y = x / mag, y / mag
    return (x, y)


class Controller:
    """Holds what the phone wants and keeps the Mac's held keys in sync with it."""

    def __init__(self, backend, args):
        self.backend = backend
        self.args = args
        self.held = set()        # keys/buttons currently down on the Mac
        self.pressed_at = {}     # key -> when it went down
        self.buttons = set()     # keys the phone's buttons are holding
        self.left = (0.0, 0.0)   # move stick, screen coords (y down)
        self.right = (0.0, 0.0)  # camera stick
        self.acc = [0.0, 0.0]    # sub-pixel mouse remainder (camera stick)
        self.look_acc = [0.0, 0.0]  # sub-pixel mouse remainder (swipe look)
        self.last_msg = time.monotonic()
        self.stale = False

    # -- phone messages -------------------------------------------------
    def handle_text(self, data):
        if len(data) > MAX_MSG_CHARS:
            return
        try:
            msg = json.loads(data)
        except (ValueError, RecursionError):
            return
        if not isinstance(msg, dict):
            return
        kind = msg.get("t")
        if kind == "s":
            left, right = unit_vec(msg.get("l")), unit_vec(msg.get("m"))
            if left is None and right is None:
                return
            if left is not None:
                self.left = left
            if right is not None:
                self.right = right
        elif kind == "d":
            pair = number_pair(msg.get("d"))
            if pair is None:
                return
            self.look(*pair)
        elif kind == "b":
            key, down = msg.get("k"), msg.get("d")
            if not isinstance(key, str) or key not in macinput.KEY_NAMES:
                return
            if not isinstance(down, bool):
                return
            # Button presses go to phonepad.log only, so a button that "does
            # nothing" can be traced: did the press reach the Mac at all?
            log(f"button {key} {'down' if down else 'up'}", console=False)
            if key in macinput.WHEEL:
                # The wheel has no "held" state: one notch per press.
                if down:
                    self._send(self.backend.scroll, macinput.WHEEL[key])
            elif down:
                self.buttons.add(key)
            else:
                self.buttons.discard(key)
        else:
            return
        self.last_msg = time.monotonic()
        self.stale = False
        self.sync()

    # -- key bookkeeping ------------------------------------------------
    def wanted(self):
        want = set(self.buttons)
        th = self.args.threshold
        lx, ly = self.left
        if ly < -th:
            want.add("w")
        elif ly > th:
            want.add("s")
        if lx < -th:
            want.add("a")
        elif lx > th:
            want.add("d")
        if self.args.camera == "arrows":
            rx = self.right[0]
            if rx < -th:
                want.add("left")
            elif rx > th:
                want.add("right")
        return want

    def sync(self, force=False):
        """Press newly wanted keys, release ones no longer wanted. Modifiers go
        down first and come up last so combos land the right way round.

        A key released less than MIN_PRESS after it went down stays down until
        then (tick() finishes the job): a touch near the screen edge can arrive
        as down+up in the same instant, and games polling once a frame miss it.
        """
        want = self.wanted()
        now = time.monotonic()
        is_mod = lambda k: k in macinput.MODIFIERS
        for key in sorted(self.held - want, key=is_mod):
            if not force and now - self.pressed_at.get(key, 0.0) < MIN_PRESS:
                continue
            self._send(self.backend.release, key)
            self.held.discard(key)
        for key in sorted(want - self.held, key=lambda k: not is_mod(k)):
            self._send(self.backend.press, key)
            self.held.add(key)
            self.pressed_at[key] = now

    def look(self, dx, dy):
        """Swipe-to-look: phone pixels of finger travel -> relative mouse move."""
        dx = max(-MAX_LOOK_STEP, min(MAX_LOOK_STEP, dx)) * self.args.look_sens
        dy = max(-MAX_LOOK_STEP, min(MAX_LOOK_STEP, dy)) * self.args.look_sens
        if self.args.invert_y:
            dy = -dy
        self.look_acc[0] += dx
        self.look_acc[1] += dy
        ix, iy = int(self.look_acc[0]), int(self.look_acc[1])
        if ix or iy:
            self.look_acc[0] -= ix
            self.look_acc[1] -= iy
            self._send(self.backend.move, ix, iy)

    def release_all(self, reason=None, force=False):
        busy = bool(self.held) or self.left != (0.0, 0.0) or self.right != (0.0, 0.0)
        self.buttons.clear()
        self.left = self.right = (0.0, 0.0)
        self.acc = [0.0, 0.0]
        self.sync(force=force)
        if reason and busy:
            log(f"released everything ({reason})")

    def _send(self, fn, *a):
        try:
            fn(*a)
        except Exception as e:  # never let one bad event kill the loop
            log(f"input error: {e!r}")

    # -- 120 Hz loop ----------------------------------------------------
    def tick(self, dt):
        if not self.stale and time.monotonic() - self.last_msg > STALE_AFTER:
            self.stale = True
            self.release_all("no input for 1.5 s")
        if self.held - self.wanted():
            self.sync()         # finish releases held back by MIN_PRESS
        if self.args.camera != "mouse":
            return
        x, y = self.right
        mag = math.hypot(x, y)
        dz = self.args.deadzone
        if mag <= dz:
            self.acc = [0.0, 0.0]
            return
        speed = ((mag - dz) / (1.0 - dz)) ** self.args.curve * self.args.sens
        vy = y if not self.args.invert_y else -y
        self.acc[0] += x / mag * speed * dt
        self.acc[1] += vy / mag * speed * dt
        ix, iy = int(self.acc[0]), int(self.acc[1])   # truncates toward zero
        if ix or iy:
            self.acc[0] -= ix
            self.acc[1] -= iy
            self._send(self.backend.move, ix, iy)


# -- HTTP / WebSocket ---------------------------------------------------
async def index(request):
    return web.FileResponse(INDEX_FILE, headers={"Cache-Control": "no-cache"})


async def ca_cert(request):
    """The local CA, for installing on the iPhone (Settings shows it as a profile)."""
    ca = CERT_DIR / "ca.crt"
    if not ca.exists():
        raise web.HTTPNotFound(text="HTTPS is off, so there is no certificate.\n")
    return web.Response(body=ca.read_bytes(), content_type="application/x-x509-ca-cert")


async def ca_profile(request):
    """The local CA wrapped in a configuration profile: iPhone Safari offers to
    install this directly (Settings → Profile Downloaded)."""
    ca = CERT_DIR / "ca.crt"
    if not ca.exists():
        raise web.HTTPNotFound(text="HTTPS is off, so there is no certificate.\n")
    pem = ca.read_text()
    der = ssl.PEM_cert_to_DER_cert(pem)
    # Stable IDs, so reinstalling replaces the old profile instead of adding another.
    ns = uuid.uuid5(uuid.NAMESPACE_URL, "phonepad:" + pem)
    host = local_hostname()
    profile = {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": f"local.phonepad.{host}",
        "PayloadUUID": str(uuid.uuid5(ns, "profile")).upper(),
        "PayloadDisplayName": "PhonePad",
        "PayloadDescription": f"Lets this iPhone open the secure PhonePad page served by {host}.",
        "PayloadOrganization": "PhonePad",
        "PayloadContent": [{
            "PayloadType": "com.apple.security.root",
            "PayloadVersion": 1,
            "PayloadIdentifier": f"local.phonepad.{host}.ca",
            "PayloadUUID": str(uuid.uuid5(ns, "ca")).upper(),
            "PayloadDisplayName": "PhonePad local CA",
            "PayloadCertificateFileName": "PhonePad-CA.cer",
            "PayloadContent": der,
        }],
    }
    return web.Response(body=plistlib.dumps(profile), content_type="application/x-apple-aspen-config")


async def info(request):
    return web.json_response({"https_port": request.app[STATE]["https_port"]},
                             headers={"Cache-Control": "no-cache"})


async def websocket(request):
    st = request.app[STATE]
    ctrl = st["ctrl"]
    given = request.query.get("token", "")
    if not hmac.compare_digest(given.encode(), st["token"].encode()):
        log(f"rejected {request.remote}: wrong pairing token")
        return web.Response(status=403, text="wrong token\n")

    ws = web.WebSocketResponse(heartbeat=10.0, max_msg_size=4096)
    await ws.prepare(request)

    old = st["ws"]
    st["ws"] = ws
    if old is not None and not old.closed:
        ctrl.release_all("another phone connected")
        await old.close(code=4000, message=b"replaced by another phone")
    ctrl.last_msg = time.monotonic()
    ctrl.stale = False
    log(f"phone connected from {request.remote}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                ctrl.handle_text(msg.data)
    finally:
        if st["ws"] is ws:
            st["ws"] = None
            ctrl.release_all("phone disconnected")
            log("phone disconnected")
    return ws


async def tick_loop(app):
    ctrl = app[STATE]["ctrl"]
    period = 1.0 / TICK_HZ
    last = time.monotonic()
    while True:
        await asyncio.sleep(period)
        now = time.monotonic()
        dt, last = min(now - last, 0.05), now
        ctrl.tick(dt)


async def on_startup(app):
    app[STATE]["tick"] = asyncio.create_task(tick_loop(app))


async def on_shutdown(app):
    st = app[STATE]
    st["tick"].cancel()
    if st["ws"] is not None:
        await st["ws"].close(code=1001, message=b"server shutting down")
    st["ctrl"].release_all("server shutting down", force=True)


@web.middleware
async def access_log(request, handler):
    """Every request goes to phonepad.log (never the token), so "the link doesn't
    work" can be told apart: did the phone reach the Mac at all?"""
    log(f"{request.method} {request.path} from {request.remote}", console=False)
    return await handler(request)


def build_app(args, backend, token):
    app = web.Application(middlewares=[access_log])
    # All mutable state lives in this one dict, set before the app starts.
    app[STATE] = {"ctrl": Controller(backend, args), "token": token, "ws": None, "tick": None,
                  "https_port": None}
    app.router.add_get("/", index)
    app.router.add_get("/ca.crt", ca_cert)
    app.router.add_get("/phonepad.mobileconfig", ca_profile)
    app.router.add_get("/info", info)
    app.router.add_get("/ws", websocket)
    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)
    return app


# -- startup banner -----------------------------------------------------
def _udp_source(family, target):
    s = socket.socket(family, socket.SOCK_DGRAM)
    try:
        s.connect((target, 80))   # no packet is sent; this just picks a route
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _usable_v4(addr):
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    # 192.0.0.x is the CLAT stub macOS uses on IPv6-only networks; not reachable.
    return (ip.is_private and not ip.is_loopback and not ip.is_link_local
            and ip not in ipaddress.ip_network("192.0.0.0/24"))


def lan_addresses():
    """(ipv4, ipv6) that a phone on the same network can reach; either may be None."""
    v4 = _udp_source(socket.AF_INET, "8.8.8.8")
    if not (v4 and _usable_v4(v4)):
        v4 = None
        try:
            out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "inet" and _usable_v4(parts[1]):
                v4 = parts[1]
                break
    v6 = _udp_source(socket.AF_INET6, "2001:4860:4860::8888")
    if v6:
        ip = ipaddress.ip_address(v6.split("%")[0])
        v6 = str(ip) if not (ip.is_link_local or ip.is_loopback) else None
    return v4, v6


def all_addresses():
    """Every address on this Mac a phone might use (for the HTTPS certificate)."""
    found = {"127.0.0.1", "::1"}
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ("inet", "inet6"):
            continue
        try:
            ip = ipaddress.ip_address(parts[1].split("%")[0])
        except ValueError:
            continue
        if ip.is_link_local or (ip.version == 4 and not _usable_v4(str(ip)) and not ip.is_loopback):
            continue
        found.add(str(ip))
    return found


def local_hostname():
    try:
        name = subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True,
                              text=True, timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or socket.gethostname().split(".")[0]


def build_links(port, token, scheme="http", extra=""):
    v4, v6 = lan_addresses()
    q = f":{port}/?token={token}{extra}"
    links = []
    if v4:
        links.append(f"{scheme}://{v4}{q}")
    if v6:
        links.append(f"{scheme}://[{v6}]{q}")
    links.append(f"{scheme}://{local_hostname()}.local{q}")
    return links


def setup_tls():
    """SSLContext for the HTTPS listener, or None (with a printed reason)."""
    try:
        host = local_hostname()
        cert, key, _ = certs.ensure(CERT_DIR, [f"{host}.local", "localhost"], all_addresses(), host)
    except ImportError:
        print("  (HTTPS is off: run start.command once more to install the 'cryptography' package.)")
        return None
    except Exception as e:  # never let HTTPS trouble stop the controller itself
        print(f"  (HTTPS is off: couldn't create a certificate: {e})")
        return None
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


def qr_lines(url):
    """QR code drawn with half blocks and explicit black/white colours, so it
    scans whether the terminal theme is light or dark."""
    import qrcode

    qr = qrcode.QRCode(border=2, error_correction=qrcode.constants.ERROR_CORRECT_L)
    qr.add_data(url)
    qr.make(fit=True)
    m = qr.get_matrix()
    if len(m) % 2:
        m.append([False] * len(m[0]))
    lines = []
    for r in range(0, len(m), 2):
        row = "".join(f"\x1b[{'30' if top else '97'};{'40' if bot else '107'}m▀"
                      for top, bot in zip(m[r], m[r + 1]))
        lines.append("  " + row + "\x1b[0m")
    return lines


def print_banner(args, token, ax_ok, https_port):
    links = build_links(args.port, token)
    print()
    print("  PhonePad" + ("  (DRY RUN: input is printed, not sent)" if args.dry_run else ""))
    print()
    for line in qr_lines(links[0]):
        print(line)
    print()
    print("  Scan with the iPhone camera, or open one of these in Safari:")
    for link in links:
        print(f"    {link}")
    print()
    print("  Phone and Mac must be on the same Wi-Fi. Then click into the Roblox window.")
    if https_port:
        print()
        print("  Mouse mode with phone motion needs the secure page (one-time setup on the")
        print("  phone: Mouse → Set up motion walks you through it). Secure link:")
        print(f"    {build_links(https_port, token, 'https', '&mode=mouse')[0]}")
    if not args.dry_run and not ax_ok:
        app = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm"}.get(
            os.environ.get("TERM_PROGRAM", ""), "the app you started this from (e.g. Terminal)")
        print()
        print("  \x1b[33m! Accessibility permission is missing, so macOS will ignore the input.\x1b[0m")
        print("    1. Open System Settings → Privacy & Security → Accessibility")
        print(f"    2. Turn on {app} (use + to add it if it isn't listed)")
        print("    3. Stop this server with Ctrl+C and run start.command again")
    print()
    print("  Ctrl+C to stop.")
    print(flush=True)


# -- main -----------------------------------------------------------------
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Use your iPhone as a game controller for this Mac.")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--dry-run", action="store_true", help="print input events instead of sending them")
    p.add_argument("--new-token", action="store_true", help="make a new pairing token (re-scan the QR)")
    p.add_argument("--threshold", type=float, default=0.35, help="move stick push needed for W/A/S/D")
    p.add_argument("--deadzone", type=float, default=0.12, help="camera stick dead zone")
    p.add_argument("--curve", type=float, default=1.6, help="camera response curve (1 = linear)")
    p.add_argument("--sens", type=float, default=900, help="camera speed in px/s at full tilt")
    p.add_argument("--look-sens", type=float, default=2.0,
                   help="swipe-to-look speed: Mac px per phone px of finger travel")
    p.add_argument("--edge", choices=("recenter", "clamp"), default="recenter",
                   help="when the cursor reaches the game window's edge: jump back to the "
                        "centre (default, best for aiming) or stop at the edge")
    p.add_argument("--https-port", type=int, default=None,
                   help="port for the secure page used by motion control (default: --port + 1)")
    p.add_argument("--no-https", action="store_true", help="don't start the secure page")
    p.add_argument("--invert-y", action="store_true", help="invert camera up/down")
    p.add_argument("--camera", choices=("mouse", "arrows"), default="mouse",
                   help="camera stick moves the mouse, or holds the ←/→ arrow keys")
    args = p.parse_args(argv)
    if not 0.0 <= args.deadzone < 0.95:
        p.error("--deadzone must be between 0 and 0.95")
    if not 0.0 < args.threshold < 1.0:
        p.error("--threshold must be between 0 and 1")
    if args.curve <= 0 or args.sens < 0 or args.look_sens < 0:
        p.error("--curve must be > 0, and --sens / --look-sens >= 0")
    return args


async def serve(args):
    try:
        if LOG_FILE.stat().st_size > 2_000_000:   # keep the log from growing forever
            LOG_FILE.write_text("")
    except OSError:
        pass
    token = load_token(args.new_token)
    backend = macinput.get_backend(args.dry_run, args.edge)
    ax_ok = args.dry_run or macinput.accessibility_ok(prompt=True)
    app = build_app(args, backend, token)

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host=None, port=args.port)   # all interfaces, IPv4 + IPv6
    try:
        await site.start()
    except OSError as e:
        await runner.cleanup()
        raise SystemExit(f"Can't listen on port {args.port}: {e.strerror}. "
                         f"Is PhonePad already running? Or try --port {args.port + 1}.")

    https_port = None
    if not args.no_https:
        ctx = setup_tls()
        port2 = args.https_port or args.port + 1
        if ctx is not None:
            try:
                await web.TCPSite(runner, host=None, port=port2, ssl_context=ctx).start()
                https_port = port2
            except OSError as e:
                print(f"  (HTTPS is off: can't listen on port {port2}: {e.strerror})")
    app[STATE]["https_port"] = https_port

    print_banner(args, token, ax_ok, https_port)
    log(f"listening on port {args.port}" + (f" and {https_port} (https)" if https_port else "")
        + f" ({backend.name}); accessibility {'granted' if ax_ok else 'MISSING'}")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        app[STATE]["ctrl"].release_all("server shutting down", force=True)
        await runner.cleanup()
        log("stopped")


def main():
    sys.stdout.reconfigure(line_buffering=True)
    asyncio.run(serve(parse_args()))


if __name__ == "__main__":
    main()
