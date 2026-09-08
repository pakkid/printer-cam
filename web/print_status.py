#!/usr/bin/env python3
"""Sanitised print-progress endpoint for the camera overlay.

Moonraker is never exposed to the browser. It has no CORS headers, and more to
the point its API can *control* the printer (cancel a print, run arbitrary
gcode, emergency stop). So this asks it for the few fields the overlay needs
and returns nothing else:

    {"printing": false}
    {"printing": true, "progress": 0.42, "elapsed_s": 1234,
     "remaining_s": 1700, "filament_g": 12.3}

Two of those have to be derived, because the K2's Moonraker reports
`slicer: Unknown` and leaves `estimated_time`, `filament_total` and
`filament_weight_total` null -- it does not parse slicer metadata at all:

  remaining_s  from elapsed time and progress, not from the slicer's estimate.
               Unreliable in the first moments of a print, so it is reported as
               null until progress passes 0.5%.
  filament_m   from the extruded length in mm, which is all Klipper tracks.
               Just a unit conversion, so it involves no guesswork.

Grams need the filament's density, and the density needs to know what is
loaded. Moonraker will not say: its filament_rack reports only an undocumented
Creality code ("001601"), and the CFS "box" object reports material_type only
while the CFS is connected.

Creality's own proprietary WebSocket on port 9999 does say, which is how
Creality Print and OrcaSlicer know. Asking it {"method":"get",
"params":{"boxsInfo":1}} returns the loaded spool already decoded:

    box id=0 (the spool holder; 1-4 are CFS boxes)
      vendor='Creality' type='PLA' name='Soleyin Ultra PLA' color='#0ffffff'

So the type is read from there and mapped to a density, which makes grams
correct without anyone configuring anything. FILAMENT_DENSITY still overrides
it, and if port 9999 says nothing usable then grams are simply omitted rather
than guessed.

Protocol reverse-engineered by DaviBe92/k2-websocket-re. It is not documented
by Creality and may change with firmware, hence all the defensive handling.
"""
import base64
import json
import math
import os
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOONRAKER = "http://%s:%s/printer/objects/query?print_stats&display_status&virtual_sdcard" % (
    os.environ.get("PRINTER_IP", "192.168.1.17"),
    os.environ.get("MOONRAKER_PORT", "7125"),
)
PRINTER_HOST = os.environ.get("PRINTER_IP", "192.168.1.17")
CREALITY_WS_PORT = int(os.environ.get("CREALITY_WS_PORT", "9999"))
LISTEN_PORT = int(os.environ.get("STATUS_PORT", "8099"))
POLL_SECONDS = 2.0        # a shared cache, so 50 viewers still means one poll
SPOOL_SECONDS = 30.0      # the loaded spool changes far less often
TIMEOUT = 4.0

# Overrides whatever the printer reports, for anyone printing something the
# table below does not cover.
_density = os.environ.get("FILAMENT_DENSITY", "").strip()
DENSITY_OVERRIDE = float(_density) if _density else None   # g/cm3
DIAMETER = float(os.environ.get("FILAMENT_DIAMETER", "1.75") or 1.75)   # mm

# Typical densities in g/cm3. Filled types are heavier, hence the separate
# entries; anything not listed falls through to no grams rather than a guess.
DENSITIES = {
    "PLA": 1.24, "PLA+": 1.24, "PLA-CF": 1.30, "SILK": 1.24, "PLA-SILK": 1.24,
    "PETG": 1.27, "PET": 1.27, "PETG-CF": 1.30,
    "ABS": 1.04, "ABS-CF": 1.11, "ASA": 1.07,
    "TPU": 1.21, "TPE": 1.20,
    "PA": 1.14, "NYLON": 1.14, "PA-CF": 1.19,
    "PC": 1.20, "HIPS": 1.04, "PVA": 1.23, "PVB": 1.09,
}


def grams_per_mm(density):
    """grams = volume * density, and 1 cm3 is 1000 mm3."""
    return math.pi * (DIAMETER / 2) ** 2 * density / 1000.0

_lock = threading.Lock()
_cache = {"at": 0.0, "payload": {"printing": False}}
_spool_lock = threading.Lock()
_spool = {"at": 0.0, "info": None}


# --- Creality's proprietary WebSocket (port 9999) -------------------------
# Only used to ask what filament is loaded. One request, {"method":"get"}, and
# the connection is closed again. A hand-rolled client because this needs no
# dependencies and only ever sends that single frame.

def _ws_send_text(sock, text):
    payload = text.encode()
    mask = os.urandom(4)
    header = bytearray([0x81])
    n = len(payload)
    if n < 126:
        header.append(0x80 | n)
    elif n < 65536:
        header.append(0x80 | 126)
        header += struct.pack(">H", n)
    else:
        header.append(0x80 | 127)
        header += struct.pack(">Q", n)
    sock.sendall(bytes(header) + mask
                 + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))


def _ws_boxs_info():
    """Returns the printer's materialBoxs list, or None."""
    sock = socket.create_connection((PRINTER_HOST, CREALITY_WS_PORT), timeout=TIMEOUT)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall((
            f"GET / HTTP/1.1\r\nHost: {PRINTER_HOST}:{CREALITY_WS_PORT}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())

        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head, buf = buf.split(b"\r\n\r\n", 1)
        if b"101" not in head.split(b"\r\n")[0]:
            return None

        _ws_send_text(sock, json.dumps({"method": "get", "params": {"boxsInfo": 1}}))
        sock.settimeout(TIMEOUT)

        def need(n):
            nonlocal buf
            while len(buf) < n:
                chunk = sock.recv(65536)
                if not chunk:
                    raise EOFError
                buf += chunk
            out, buf = buf[:n], buf[n:]
            return out

        # The printer pushes unrelated state frames too, so read past them.
        for _ in range(30):
            head2 = need(2)
            opcode = head2[0] & 0x0F
            length = head2[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", need(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", need(8))[0]
            body = need(length)
            if opcode == 8:
                return None
            if opcode != 1:
                continue
            try:
                msg = json.loads(body.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(msg, dict) and "boxsInfo" in msg:
                return (msg["boxsInfo"] or {}).get("materialBoxs") or []
        return None
    finally:
        sock.close()


def _pick_material(boxes):
    """The loaded spool: whichever slot is selected, else the only one there."""
    slots = [
        (box, mat)
        for box in boxes or []
        for mat in (box.get("materials") or [])
        if mat.get("state") not in (0, None) and (mat.get("type") or "").strip()
    ]
    if not slots:
        return None
    for box, mat in slots:
        if mat.get("selected") == 1:
            return mat
    return slots[0][1] if len(slots) == 1 else None


def spool():
    """Cached filament info from port 9999, or None if it says nothing useful."""
    now = time.monotonic()
    with _spool_lock:
        if now - _spool["at"] < SPOOL_SECONDS:
            return _spool["info"]
    try:
        mat = _pick_material(_ws_boxs_info())
    except (OSError, ValueError, KeyError, EOFError, TimeoutError):
        mat = None
    info = None
    if mat:
        # Colours come back with a stray leading zero, e.g. "#0ffffff".
        colour = (mat.get("color") or "").strip()
        if colour.startswith("#0") and len(colour) == 8:
            colour = "#" + colour[2:]
        info = {
            "type": (mat.get("type") or "").strip().upper() or None,
            "name": (mat.get("name") or "").strip() or None,
            "color": colour if len(colour) == 7 else None,
        }
    with _spool_lock:
        _spool["at"] = time.monotonic()
        _spool["info"] = info
    return info


def _fetch():
    with urllib.request.urlopen(MOONRAKER, timeout=TIMEOUT) as r:
        return json.load(r)["result"]["status"]


def _shape(status):
    stats = status.get("print_stats") or {}
    state = stats.get("state") or ""

    # "paused" still counts as an active print; the bar should stay up.
    if state not in ("printing", "paused"):
        return {"printing": False}

    progress = (status.get("display_status") or {}).get("progress")
    if not isinstance(progress, (int, float)):
        progress = (status.get("virtual_sdcard") or {}).get("progress") or 0.0
    progress = min(max(float(progress), 0.0), 1.0)

    elapsed = float(stats.get("print_duration") or 0.0)

    # Extrapolating from <0.5% done gives absurd numbers, so say nothing.
    remaining = None
    if progress > 0.005 and elapsed > 0:
        remaining = round(elapsed * (1.0 - progress) / progress)

    used_mm = float(stats.get("filament_used") or 0.0)

    payload = {
        "printing": True,
        "paused": state == "paused",
        "progress": round(progress, 4),
        "elapsed_s": round(elapsed),
        "remaining_s": remaining,
        "filament_m": round(used_mm / 1000.0, 2),
    }

    info = spool()
    if info:
        payload["filament_type"] = info["type"]
        payload["filament_name"] = info["name"]
        payload["filament_color"] = info["color"]

    density = DENSITY_OVERRIDE
    if density is None and info and info["type"]:
        density = DENSITIES.get(info["type"])
    if density:
        payload["filament_g"] = round(used_mm * grams_per_mm(density), 1)
    return payload


def current():
    """Cached, so viewer count does not multiply load on the printer."""
    now = time.monotonic()
    with _lock:
        if now - _cache["at"] < POLL_SECONDS:
            return _cache["payload"]
    try:
        payload = _shape(_fetch())
    except (urllib.error.URLError, OSError, ValueError, KeyError, TimeoutError):
        # Printer asleep, rebooting, or unreachable. The overlay treats this
        # the same as "not printing" and simply stays hidden.
        payload = {"printing": False}
    with _lock:
        _cache["at"] = time.monotonic()
        _cache["payload"] = payload
    return payload


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "printer-cam-status"

    def do_GET(self):
        if self.path.split("?")[0] != "/print":
            self.send_error(404)
            return
        body = json.dumps(current()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass    # nginx already logs the request


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
