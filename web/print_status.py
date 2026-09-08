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
  filament_g   from the extruded length, which is all Klipper tracks, using the
               filament diameter and density below. Change them if you print
               something other than 1.75 mm PLA.
"""
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOONRAKER = "http://%s:%s/printer/objects/query?print_stats&display_status&virtual_sdcard" % (
    os.environ.get("PRINTER_IP", "192.168.1.17"),
    os.environ.get("MOONRAKER_PORT", "7125"),
)
DIAMETER = float(os.environ.get("FILAMENT_DIAMETER", "1.75"))   # mm
DENSITY = float(os.environ.get("FILAMENT_DENSITY", "1.24"))     # g/cm3, PLA
LISTEN_PORT = int(os.environ.get("STATUS_PORT", "8099"))
POLL_SECONDS = 2.0        # a shared cache, so 50 viewers still means one poll
TIMEOUT = 4.0

# Klipper reports extruded filament as a length in mm. grams = volume * density,
# and 1 cm3 is 1000 mm3.
GRAMS_PER_MM = math.pi * (DIAMETER / 2) ** 2 * DENSITY / 1000.0

_lock = threading.Lock()
_cache = {"at": 0.0, "payload": {"printing": False}}


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

    return {
        "printing": True,
        "paused": state == "paused",
        "progress": round(progress, 4),
        "elapsed_s": round(elapsed),
        "remaining_s": remaining,
        "filament_g": round(used_mm * GRAMS_PER_MM, 1),
    }


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
