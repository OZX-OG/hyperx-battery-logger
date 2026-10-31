"""
HyperX Cloud III S Wireless battery logger.

Usage:
    python hyperx_battery_logger.py
    python hyperx_battery_logger.py --interval 30
    python hyperx_battery_logger.py --no-tray
    python hyperx_battery_logger.py --once
"""

import argparse
import json
import logging
import logging.handlers
import os
import sys
import threading
import time
import webbrowser
from datetime import datetime

try:
    import hid
    hid.device
except (ImportError, AttributeError):
    sys.exit("This needs the 'hidapi' package (not 'hid'): python -m pip install hidapi")

try:
    import msvcrt
except ImportError:
    msvcrt = None

VENDOR_ID = 0x03F0
PRODUCT_ID = 0x06BE
USAGE_PAGE = 0x01C0

HEADER = bytes([0x0C, 0x02, 0x03, 0x01, 0x00])
CMD_BATTERY = 0x06
CMD_MIC = 0x04
REPORT_LEN = 52

PUSH = bytes([0x0D, 0x02, 0x03, 0x00])
EV_BATTERY = 0x01
EV_MIC = 0x03
EV_CHARGING = 0x0A
EV_POWER = 0x0C

HERE = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(HERE, "data")
LOG_PATH = os.path.join(DATA_DIR, "battery_log.jsonl")
OLD_JSON_PATH = os.path.join(DATA_DIR, "battery_log.json")
JS_PATH = os.path.join(DATA_DIR, "battery_data.js")
LIVE_PATH = os.path.join(DATA_DIR, "battery_live.js")
LOCK_PATH = os.path.join(DATA_DIR, "logger.lock")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
ERROR_LOG = os.path.join(DATA_DIR, "logger.log")
DASHBOARD = os.path.join(HERE, "battery_dashboard.html")

HEARTBEAT = 300
GAP = 12 * 60
RETRY_AFTER_MISS = 5
PORT = 47821

LOW_ALERTS = [20, 10]
VJUMP = 25

log = logging.getLogger("hyperx")


def find_path():
    for d in hid.enumerate(VENDOR_ID, PRODUCT_ID):
        if d["usage_page"] == USAGE_PAGE:
            return d["path"]
    return None


class Headset:
    def __init__(self, charging=None):
        self.dev = None
        self.charging = charging
        self.muted = None

    def open(self):
        if self.dev:
            return True
        path = find_path()
        if path is None:
            return False
        dev = hid.device()
        try:
            dev.open_path(path)
            dev.set_nonblocking(True)
        except OSError:
            return False
        self.dev = dev
        return True

    def close(self):
        if self.dev:
            self.dev.close()
            self.dev = None

    def _handle(self, r):
        if len(r) < 6 or bytes(r[:4]) != PUSH:
            return None
        event, value = r[4], r[5]
        if event == EV_CHARGING:
            self.charging = bool(value)
        elif event == EV_MIC:
            self.muted = bool(value)
        return event

    def pump(self, seconds):
        seen = set()
        end = time.time() + seconds
        try:
            while time.time() < end:
                r = self.dev.read(64)
                if r:
                    code = self._handle(r)
                    if code is not None:
                        seen.add(code)
                else:
                    time.sleep(0.05)
        except OSError:
            self.close()
        return seen

    def query(self, cmd, timeout=1.0):
        self.dev.write(HEADER + bytes([cmd]) + bytes(REPORT_LEN - len(HEADER) - 1))
        end = time.time() + timeout
        while time.time() < end:
            r = self.dev.read(64)
            if r and bytes(r[:5]) == HEADER and r[5] == cmd:
                return r
            if r:
                self._handle(r)
            else:
                time.sleep(0.01)
        return None

    def status(self):
        if not self.open():
            return None
        try:
            r = self.query(CMD_BATTERY)
            if r is None or r[6] > 100:
                return None
            m = self.query(CMD_MIC, timeout=0.5)
            if m is not None:
                self.muted = bool(m[6])
        except OSError:
            self.close()
            return None
        status = {"pct": r[6], "mv": (r[7] << 8) | r[8]}
        if self.charging is not None:
            status["chg"] = int(self.charging)
        if self.muted is not None:
            status["mic"] = "muted" if self.muted else "live"
        return status


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def parse_time(s):
    return datetime.fromisoformat(s).astimezone()


def make_reading(status):
    if status is None:
        return {"t": now_iso(), "state": "disconnected"}
    return {"t": now_iso(), "state": "connected", **status}


def seconds_between(a, b):
    return (parse_time(b["t"]) - parse_time(a["t"])).total_seconds()


def should_save(reading, last):
    if last is None or reading["state"] != last["state"]:
        return True
    if reading["state"] == "disconnected":
        return False
    if any(reading.get(k) != last.get(k) for k in ("pct", "chg", "mic")):
        return True
    return seconds_between(last, reading) >= HEARTBEAT


def write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 19:
                os.remove(tmp)
                raise
            time.sleep(0.1)


def js_line(reading):
    return "BATTERY_READINGS.push(" + json.dumps(reading, separators=(",", ":")) + ");\n"


def read_log(path):
    readings, bad = [], 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                    r["t"], r["state"]
                    readings.append(r)
                except (ValueError, TypeError, KeyError):
                    bad += 1
    except FileNotFoundError:
        pass
    return readings, bad


class Store:
    def __init__(self):
        self.count = 0
        self.last = None

    def load(self):
        self._migrate_old_json()
        readings, bad = read_log(LOG_PATH)
        if bad:
            backup = f"{LOG_PATH}.bad-{datetime.now():%Y%m%d-%H%M%S}"
            os.replace(LOG_PATH, backup)
            log.warning("skipped %d damaged lines in the log, original kept as %s", bad, backup)
            write_atomic(LOG_PATH, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in readings))
        write_atomic(JS_PATH, "window.BATTERY_READINGS = [];\n" + "".join(js_line(r) for r in readings))
        self.count = len(readings)
        self.last = readings[-1] if readings else None

    def _migrate_old_json(self):
        if not os.path.exists(OLD_JSON_PATH) or os.path.exists(LOG_PATH):
            return
        try:
            with open(OLD_JSON_PATH, encoding="utf-8") as f:
                readings = json.load(f)["readings"]
        except (ValueError, KeyError, TypeError):
            log.warning("could not read the old battery_log.json, starting a new log")
            readings = []
        write_atomic(LOG_PATH, "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in readings))
        os.replace(OLD_JSON_PATH, OLD_JSON_PATH + ".bak")
        log.info("moved %d readings from battery_log.json to battery_log.jsonl", len(readings))

    def append(self, reading):
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(reading, separators=(",", ":")) + "\n")
        with open(JS_PATH, "a", encoding="utf-8") as f:
            f.write(js_line(reading))
        self.count += 1
        self.last = reading

    def save_live(self, live):
        write_atomic(LIVE_PATH, "window.BATTERY_LIVE = " + json.dumps({**live, "count": self.count}) + ";\n")


def direction(prev, cur, last_dir):
    if cur["state"] != "connected":
        return None
    if "chg" in cur:
        return "charging" if cur["chg"] else "discharging"
    if prev is None or prev["state"] != "connected" or seconds_between(prev, cur) > GAP:
        return last_dir
    dp, dv = cur["pct"] - prev["pct"], cur["mv"] - prev["mv"]
    if dp > 0 or (dp == 0 and dv >= VJUMP):
        return "charging"
    if dp < 0 or (dp == 0 and dv <= -VJUMP):
        return "discharging"
    return last_dir


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return {"alert80": False, **json.load(f)}
    except (FileNotFoundError, ValueError, TypeError):
        return {"alert80": False}


def save_settings(settings):
    write_atomic(SETTINGS_PATH, json.dumps(settings, indent=1))


def alerts(prev, cur, fired, alert80=False):
    if cur["state"] != "connected":
        return None
    pct = cur["pct"]
    for level in LOW_ALERTS:
        if pct >= level + 3:
            fired.discard(level)
    if pct < 75:
        fired.discard(80)
    if pct < 95:
        fired.discard(100)
    if prev is None or "pct" not in prev:
        return None
    charging = cur.get("chg") != 0
    crossed = [lv for lv in LOW_ALERTS if prev["pct"] > lv >= pct and lv not in fired]
    if crossed:
        fired.update(crossed)
        return ("Headset battery low", f"Only {pct}% left. Time to charge your HyperX.")
    if alert80 and charging and prev["pct"] < 80 <= pct < 100 and 80 not in fired:
        fired.add(80)
        return ("Headset at 80%", "Unplug now to make the battery last longer.")
    if charging and prev["pct"] < 100 == pct and 100 not in fired:
        fired.add(100)
        return ("Headset fully charged", "Your HyperX is at 100%. You can unplug the cable.")
    return None


class Tray:
    def __init__(self, stop, force, settings):
        import pystray
        from PIL import Image, ImageDraw, ImageFont
        self.Image, self.ImageDraw, self.ImageFont = Image, ImageDraw, ImageFont
        self.stop = stop
        self.settings = settings
        self.status = "Starting..."
        self._fit = {}
        menu = pystray.Menu(
            pystray.MenuItem(lambda _: self.status, None, enabled=False),
            pystray.MenuItem("Check now", lambda: force.set()),
            pystray.MenuItem("Open dashboard", lambda: webbrowser.open(DASHBOARD), default=True),
            pystray.MenuItem("Open data folder", lambda: os.startfile(DATA_DIR)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Notify at 80% while charging", self.toggle_alert80,
                             checked=lambda _: self.settings["alert80"]),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit logger", self.quit),
        )
        self.icon = pystray.Icon("hyperx_battery", self.draw(None, None), "HyperX battery", menu)

    def font(self, text, size):
        try:
            if len(text) < 3:
                return self.ImageFont.truetype("arialbd.ttf", size)
            f = self.ImageFont.truetype("bahnschrift.ttf", size)
            f.set_variation_by_name("Bold Condensed")
            return f
        except (OSError, ValueError):
            return self.ImageFont.truetype("arialbd.ttf", size)

    def fitted_font(self, text, height):
        key = (text, height)
        if key not in self._fit:
            for size in range(80, 8, -1):
                f = self.font(text, size)
                l, t, r, b = f.getbbox(text)
                if r - l <= 62 and b - t <= height - 4:
                    break
            self._fit[key] = f
        return self._fit[key]

    def draw(self, pct, dir_, mic=None):
        if pct is None:
            color, text = (110, 110, 110), "-"
        else:
            color = ((42, 120, 214) if dir_ == "charging" else
                     (208, 59, 59) if pct <= 20 else
                     (215, 145, 0) if pct <= 50 else (12, 140, 12))
            text = str(pct)
        img = self.Image.new("RGBA", (64, 64), color)
        d = self.ImageDraw.Draw(img)
        bottom = 64
        if mic is not None:
            bottom = 46
            d.rectangle((0, 46, 63, 48), fill=(15, 15, 15))
            d.rectangle((0, 49, 63, 63), fill=(235, 40, 40) if mic == "muted" else (40, 220, 70))
        f = self.fitted_font(text, bottom)
        l, t, r, b = f.getbbox(text)
        d.text(((64 - (r - l)) / 2 - l, (bottom - (b - t)) / 2 - t), text, font=f, fill="white")
        return img

    def update(self, reading, dir_):
        if reading["state"] != "connected":
            self.status = "Headset off / not connected"
            self.icon.icon = self.draw(None, None)
        else:
            parts = [f"{reading['pct']}%",
                     {"charging": "Charging", "discharging": "On battery"}.get(dir_, "Connected")]
            if "mic" in reading:
                parts.append("Mic muted" if reading["mic"] == "muted" else "Mic on")
            parts.append(f"{reading['mv'] / 1000:.2f} V")
            self.status = " - ".join(parts)
            self.icon.icon = self.draw(reading["pct"], dir_, reading.get("mic"))
        self.icon.title = "HyperX Cloud III S: " + self.status
        self.icon.update_menu()

    def notify(self, title, message):
        self.icon.notify(message, title)

    def toggle_alert80(self):
        self.settings["alert80"] = not self.settings["alert80"]
        save_settings(self.settings)

    def quit(self):
        self.stop.set()
        self.icon.stop()

    def run(self, worker):
        def setup(icon):
            icon.visible = True
            threading.Thread(target=worker, daemon=True).start()
        self.icon.run(setup=setup)


def start_check_server(force):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("Origin") not in (None, "null"):
                self.send_response(403)
            elif self.path == "/check":
                force.set()
                self.send_response(204)
            else:
                self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        log.warning("port %d is in use, the dashboard's Check now button is disabled", PORT)
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def acquire_lock():
    f = open(LOCK_PATH, "w")
    try:
        if msvcrt:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return None
    return f


def setup_logging():
    handler = logging.handlers.RotatingFileHandler(ERROR_LOG, maxBytes=256_000, backupCount=1,
                                                   encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(handler)
    if sys.stderr:
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--interval", type=int, default=60, help="seconds between checks (default 60)")
    ap.add_argument("--once", action="store_true", help="print one reading and exit")
    ap.add_argument("--no-tray", action="store_true", help="no tray icon or notifications")
    args = ap.parse_args()

    if args.once:
        hs = Headset()
        reading = make_reading(hs.status())
        hs.close()
        print(reading)
        return 0 if reading["state"] == "connected" else 1

    os.makedirs(DATA_DIR, exist_ok=True)
    setup_logging()
    lock = acquire_lock()
    if lock is None:
        print("Another logger is already running.")
        return 1

    store = Store()
    store.load()
    settings = load_settings()
    stop = threading.Event()
    force = threading.Event()
    server = start_check_server(force)
    tray = None if args.no_tray else Tray(stop, force, settings)

    def worker():
        prev = store.last if store.last and store.last["state"] == "connected" else None
        last_chg = None
        if prev and "chg" in prev and seconds_between(prev, make_reading(None)) <= GAP:
            last_chg = bool(prev["chg"])
        hs = Headset(charging=last_chg)
        dir_ = None
        off_since = None
        missed = False
        fired = set()
        next_check = 0
        while not stop.is_set():
            try:
                if force.is_set() or time.time() >= next_check:
                    force.clear()
                    status = hs.status()
                    was_connected = store.last is not None and store.last["state"] == "connected"
                    if status is None and was_connected and not missed:
                        missed = True
                        next_check = time.time() + RETRY_AFTER_MISS
                        continue
                    missed = False
                    reading = make_reading(status)
                    if reading["state"] == "connected":
                        off_since = None
                    elif off_since is None:
                        off_since = time.time()
                    elif time.time() - off_since > GAP:
                        hs.charging = None
                    dir_ = direction(prev, reading, dir_)
                    if should_save(reading, store.last):
                        store.append(reading)
                        print(f"{reading['t']}  {reading['state']:<12} "
                              f"{reading.get('pct', '')!s:>3}%  {reading.get('mv', '')} mV  "
                              f"{dir_ or ''} {reading.get('mic', '')}", flush=True)
                    next_check = time.time() + args.interval
                    live = {**reading, "interval": args.interval,
                            "next": datetime.fromtimestamp(next_check).astimezone().isoformat(timespec="seconds")}
                    if server:
                        live["port"] = PORT
                    store.save_live(live)
                    if tray:
                        tray.update(reading, dir_)
                        alert = alerts(prev, reading, fired, settings["alert80"])
                        if alert:
                            tray.notify(*alert)
                    if reading["state"] == "connected":
                        prev = reading
                if hs.dev:
                    if hs.pump(0.3) & {EV_CHARGING, EV_MIC, EV_POWER}:
                        next_check = time.time() + 1
                else:
                    force.wait(0.3)
            except Exception:
                log.exception("check failed, retrying in 10 s")
                hs.close()
                next_check = time.time() + 10
                stop.wait(1)
        hs.close()

    log.info("logging to %s every %ds", LOG_PATH, args.interval)
    if tray:
        tray.run(worker)
    else:
        worker()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
