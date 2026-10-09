"""End-to-end check against a --dry-run server. Run: .venv/bin/python tests/test_dryrun.py

Starts server.py --dry-run on a spare port, drives it with a WebSocket client,
and checks the printed [dry-run] events.
"""
import asyncio
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
PORT = 8799
BASE = f"http://127.0.0.1:{PORT}"
SECURE = f"https://127.0.0.1:{PORT + 1}"

lines = []
failures = []


def check(cond, what):
    print(("  ok    " if cond else "  FAIL  ") + what)
    if not cond:
        failures.append(what)


def mark():
    return len(lines)


def events(since):
    return [l.split("] ", 1)[1] for l in lines[since:] if l.startswith("[dry-run]")]


def held_after(evts):
    held = set()
    for e in evts:
        kind, _, name = e.partition(" ")
        if kind == "down":
            held.add(name)
        elif kind == "up":
            held.discard(name)
    return held


async def run(token):
    async with aiohttp.ClientSession() as http:
        async with http.get(BASE + "/") as r:
            body = await r.text()
            check(r.status == 200 and "PhonePad" in body, "GET / returns the page (200)")
            check(r.headers.get("Cache-Control") == "no-cache", "page is served no-cache")

        for bad in ("/ws?token=wrong", "/ws"):
            try:
                async with http.ws_connect(BASE + bad):
                    check(False, f"{bad} should be rejected")
            except aiohttp.WSServerHandshakeError as e:
                check(e.status == 403, f"{bad} -> 403")

        ws = await http.ws_connect(f"{BASE}/ws?token={token}")

        # Left stick up-left -> W and A; right stick right -> mouse moves right.
        m = mark()
        await ws.send_json({"t": "s", "l": [-0.8, -0.8], "m": [1.0, 0.0]})
        await asyncio.sleep(0.3)
        ev = events(m)
        check("down w" in ev and "down a" in ev, "left stick presses W and A")
        moves = [tuple(map(int, e.split()[1:])) for e in ev if e.startswith("move")]
        check(moves and all(dx > 0 and dy == 0 for dx, dy in moves),
              f"right stick moves the mouse right ({sum(dx for dx, _ in moves)} px in 0.3 s)")

        # Sticks back to centre releases W/A and stops the mouse.
        m = mark()
        await ws.send_json({"t": "s", "l": [0, 0], "m": [0, 0]})
        await asyncio.sleep(0.1)
        ev = events(m)
        check("up w" in ev and "up a" in ev, "centred stick releases W and A")

        # Button press and release.
        m = mark()
        await ws.send_json({"t": "b", "k": "e", "d": True})
        await asyncio.sleep(0.05)
        await ws.send_json({"t": "b", "k": "e", "d": False})
        await asyncio.sleep(0.05)
        check(events(m) == ["down e", "up e"], "button E press + release")

        # An instant tap (down+up together) is held for at least 60 ms.
        m = mark()
        await ws.send_str('{"t":"b","k":"c","d":true}')
        await ws.send_str('{"t":"b","k":"c","d":false}')
        await asyncio.sleep(0.02)
        early = events(m)
        await asyncio.sleep(0.1)
        check(early == ["down c"] and events(m) == ["down c", "up c"],
              "instant tap is stretched to a 60 ms press")

        # Junk is ignored and the connection survives.
        m = mark()
        for junk in ['{"t":"b","k":"rm -rf","d":true}', '{"t":"b","k":"e","d":"yes"}',
                     "not json", "[1,2,3]", '{"t":"s","l":[NaN,1],"m":[Infinity,0]}',
                     '{"t":"s","l":["1","1"]}', '{"t":"x"}', "[" * 900]:
            await ws.send_str(junk)
        await asyncio.sleep(0.2)
        check(events(m) == [], "unknown key, bad JSON and non-finite numbers are ignored")
        m = mark()
        await ws.send_json({"t": "b", "k": "space", "d": True})
        await asyncio.sleep(0.05)
        check(events(m) == ["down space"] and not ws.closed, "connection still works after junk")

        # Ctrl (Crouch in The Eastern War) is a modifier and must go down and up cleanly.
        m = mark()
        await ws.send_json({"t": "b", "k": "control", "d": True})
        await asyncio.sleep(0.1)
        await ws.send_json({"t": "b", "k": "control", "d": False})
        await asyncio.sleep(0.05)
        check(events(m) == ["down control", "up control"], "Crouch (Ctrl) press + release")

        # Swipe-to-look: finger travel x --look-sens (2.0) becomes a relative mouse move.
        m = mark()
        await ws.send_json({"t": "d", "d": [10, -5.5]})
        await ws.send_json({"t": "d", "d": [0.25, 0.25]})    # sub-pixel: carried over
        await ws.send_json({"t": "d", "d": [0.25, 0.25]})
        await ws.send_str('{"t":"d","d":[NaN,3]}')
        await ws.send_json({"t": "d", "d": [99999, 0]})      # clamped to 400 * 2
        await asyncio.sleep(0.1)
        check(events(m) == ["move 20 -11", "move 1 1", "move 800 0"],
              f"swipe look moves the mouse, drops NaN, clamps huge swipes ({events(m)})")

        # Mouse wheel: one notch per press, never left "held".
        m = mark()
        for key in ("wheel_down", "wheel_up"):
            await ws.send_json({"t": "b", "k": key, "d": True})
            await ws.send_json({"t": "b", "k": key, "d": False})
        await asyncio.sleep(0.05)
        check(events(m) == ["scroll -1", "scroll 1"], "wheel buttons scroll one notch each")

        # Out-of-range stick values are clamped to the unit circle (still just W).
        m = mark()
        await ws.send_json({"t": "s", "l": [0, -50], "m": [0, 0]})
        await asyncio.sleep(0.05)
        check(events(m) == ["down w"], "huge stick value is clamped (W only)")

        # Go quiet: everything held (W + space) must be released within ~1.5 s.
        m = mark()
        await asyncio.sleep(2.0)
        ev = events(m)
        check({"up w", "up space"} <= set(ev), "2 s of silence releases every held key")
        check(not ws.closed, "connection stays open while quiet")

        # A second phone kicks the first with close code 4000 and releases its keys.
        await ws.send_json({"t": "b", "k": "shift", "d": True})
        await asyncio.sleep(0.05)
        m = mark()
        ws2 = await http.ws_connect(f"{BASE}/ws?token={token}")
        msg = await asyncio.wait_for(ws.receive(), 2)
        await asyncio.sleep(0.1)
        check(msg.type == aiohttp.WSMsgType.CLOSE and msg.data == 4000,
              "new phone kicks the old one with close code 4000")
        check("up shift" in events(m), "kicked phone's keys are released")

        # Disconnecting releases held keys too.
        await ws2.send_json({"t": "b", "k": "mouse_left", "d": True})
        await ws2.send_json({"t": "s", "l": [1, 0], "m": [0, 0]})
        await asyncio.sleep(0.05)
        m = mark()
        await ws2.close()
        await asyncio.sleep(0.2)
        check({"up mouse_left", "up d"} <= set(events(m)), "disconnect releases everything")

        # HTTPS: the page, the CA download and a WebSocket, verified against our CA.
        async with http.get(BASE + "/info") as r:
            check((await r.json()).get("https_port") == PORT + 1, "/info reports the https port")
        async with http.get(BASE + "/ca.crt") as r:
            ca_pem = await r.read()
            check(r.status == 200 and b"BEGIN CERTIFICATE" in ca_pem
                  and r.content_type == "application/x-x509-ca-cert", "/ca.crt serves the CA")
        async with http.get(BASE + "/phonepad.mobileconfig") as r:
            import plistlib
            prof = plistlib.loads(await r.read())
            inner = prof["PayloadContent"][0]
            check(r.content_type == "application/x-apple-aspen-config"
                  and inner["PayloadType"] == "com.apple.security.root"
                  and inner["PayloadContent"] == ssl.PEM_cert_to_DER_cert(ca_pem.decode()),
                  "/phonepad.mobileconfig wraps the CA as an iPhone profile")
        ctx = ssl.create_default_context(cadata=ca_pem.decode())
        async with http.get(SECURE + "/", ssl=ctx) as r:
            check(r.status == 200, "https page verifies against the PhonePad CA")
        ws3 = await http.ws_connect(f"{SECURE}/ws?token={token}", ssl=ctx)
        m = mark()
        await ws3.send_json({"t": "b", "k": "mouse_left", "d": True})
        await ws3.send_json({"t": "b", "k": "mouse_right", "d": True})
        await asyncio.sleep(0.1)
        await ws3.send_json({"t": "b", "k": "mouse_left", "d": False})
        await ws3.send_json({"t": "b", "k": "mouse_right", "d": False})
        await asyncio.sleep(0.05)
        check(events(m) == ["down mouse_left", "down mouse_right", "up mouse_left", "up mouse_right"],
              "secure WebSocket works; left + right click held together")
        await ws3.close()
        await asyncio.sleep(0.1)

    all_events = events(0)
    check(held_after(all_events) == set(), "nothing is left held at the end")


def main():
    proc = subprocess.Popen(
        [sys.executable, "server.py", "--dry-run", "--port", str(PORT)],
        cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def pump():
        for line in proc.stdout:
            lines.append(line.rstrip("\n"))
    threading.Thread(target=pump, daemon=True).start()

    try:
        deadline = time.time() + 10
        while not any("listening on port" in l for l in lines):
            if proc.poll() is not None or time.time() > deadline:
                print("\n".join(lines))
                sys.exit("server did not start")
            time.sleep(0.05)
        token = (ROOT / ".token").read_text().strip()
        asyncio.run(run(token))
    finally:
        proc.terminate()
        proc.wait(5)
    check(proc.returncode == 0, "dry-run server stopped cleanly")
    if failures:
        # Show what the server said (minus the QR code and input events) to explain the failure.
        print("\n--- server output ---")
        for line in lines:
            if not line.startswith("[dry-run]") and "\x1b[" not in line:
                print(line)
    print(f"\n{'ALL PASSED' if not failures else f'{len(failures)} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
