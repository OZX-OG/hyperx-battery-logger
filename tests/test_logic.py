import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import hyperx_battery_logger as L


def r(t, pct=None, mv=4000, state="connected", **extra):
    reading = {"t": t, "state": state}
    if state == "connected":
        reading.update(pct=pct, mv=mv, **extra)
    return reading


class ShouldSave(unittest.TestCase):
    def test_first_reading_and_state_change(self):
        self.assertTrue(L.should_save(r("2026-10-31T10:00:00+02:00", 50), None))
        self.assertTrue(L.should_save(r("2026-10-31T10:01:00+02:00", state="disconnected"),
                                      r("2026-10-31T10:00:00+02:00", 50)))

    def test_repeated_disconnected_is_skipped(self):
        off = r("2026-10-31T10:00:00+02:00", state="disconnected")
        self.assertFalse(L.should_save(r("2026-10-31T10:30:00+02:00", state="disconnected"), off))

    def test_changes_and_heartbeat(self):
        last = r("2026-10-31T10:00:00+02:00", 50, chg=0)
        self.assertFalse(L.should_save(r("2026-10-31T10:01:00+02:00", 50, chg=0), last))
        self.assertTrue(L.should_save(r("2026-10-31T10:01:00+02:00", 49, chg=0), last))
        self.assertTrue(L.should_save(r("2026-10-31T10:01:00+02:00", 50, chg=1), last))
        self.assertTrue(L.should_save(r("2026-10-31T10:05:00+02:00", 50, chg=0), last))

    def test_old_timestamps_without_offset_still_compare(self):
        old = r("2026-10-31T10:00:00", 50)
        new = r(L.now_iso(), 50)
        L.seconds_between(old, new)


class Direction(unittest.TestCase):
    def test_cable_flag_wins(self):
        self.assertEqual(L.direction(None, r("2026-10-31T10:00:00+02:00", 50, chg=1), None), "charging")
        self.assertEqual(L.direction(None, r("2026-10-31T10:00:00+02:00", 50, chg=0), None), "discharging")

    def test_guess_from_trend(self):
        prev = r("2026-10-31T10:00:00+02:00", 50, mv=3900)
        self.assertEqual(L.direction(prev, r("2026-10-31T10:01:00+02:00", 51, mv=3905), None), "charging")
        self.assertEqual(L.direction(prev, r("2026-10-31T10:01:00+02:00", 50, mv=3860), None), "discharging")
        self.assertEqual(L.direction(prev, r("2026-10-31T10:01:00+02:00", 50, mv=3902), "charging"), "charging")

    def test_no_guess_across_a_long_gap(self):
        prev = r("2026-10-31T10:00:00+02:00", 50)
        self.assertIsNone(L.direction(prev, r("2026-10-31T11:00:00+02:00", 60), None))


class Alerts(unittest.TestCase):
    def run_levels(self, levels, chg=None, alert80=False):
        fired, out, prev = set(), [], None
        for i, pct in enumerate(levels):
            extra = {} if chg is None else {"chg": chg}
            cur = r(f"2026-10-31T10:{i:02d}:00+02:00", pct, **extra)
            a = L.alerts(prev, cur, fired, alert80)
            out.append(a[0] if a else None)
            prev = cur
        return out

    def test_low_alert_once_when_bouncing(self):
        out = self.run_levels([21, 20, 21, 20, 21, 20])
        self.assertEqual(out.count("Headset battery low"), 1)

    def test_low_alert_rearms_after_recovering(self):
        out = self.run_levels([21, 20, 23, 21, 20])
        self.assertEqual(out.count("Headset battery low"), 2)

    def test_big_drop_counts_both_levels(self):
        out = self.run_levels([21, 9, 8])
        self.assertEqual(out.count("Headset battery low"), 1)

    def test_80_only_when_enabled_and_charging(self):
        self.assertIn("Headset at 80%", self.run_levels([79, 80], chg=1, alert80=True))
        self.assertNotIn("Headset at 80%", self.run_levels([79, 80], chg=1, alert80=False))
        self.assertNotIn("Headset at 80%", self.run_levels([79, 80], chg=0, alert80=True))

    def test_full_not_when_unplugged(self):
        self.assertIn("Headset fully charged", self.run_levels([99, 100], chg=1))
        self.assertNotIn("Headset fully charged", self.run_levels([99, 100], chg=0))


class Storage(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        d = self.dir.name
        self.patch = mock.patch.multiple(
            L,
            LOG_PATH=os.path.join(d, "battery_log.jsonl"),
            OLD_JSON_PATH=os.path.join(d, "battery_log.json"),
            JS_PATH=os.path.join(d, "battery_data.js"),
            LIVE_PATH=os.path.join(d, "battery_live.js"),
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.dir.cleanup()

    def test_append_and_reload(self):
        s = L.Store()
        s.load()
        s.append(r("2026-10-31T10:00:00+02:00", 50))
        s.append(r("2026-10-31T10:05:00+02:00", 49))
        s2 = L.Store()
        s2.load()
        self.assertEqual(s2.count, 2)
        self.assertEqual(s2.last["pct"], 49)
        with open(L.JS_PATH, encoding="utf-8") as f:
            self.assertEqual(f.read().count("BATTERY_READINGS.push("), 2)

    def test_damaged_lines_are_skipped_and_backed_up(self):
        with open(L.LOG_PATH, "w", encoding="utf-8") as f:
            f.write(json.dumps(r("2026-10-31T10:00:00+02:00", 50)) + "\n")
            f.write('{"t": "2026-10-31T10:0\n')
        s = L.Store()
        s.load()
        self.assertEqual(s.count, 1)
        backups = [n for n in os.listdir(self.dir.name) if ".bad-" in n]
        self.assertEqual(len(backups), 1)

    def test_old_json_is_migrated(self):
        with open(L.OLD_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump({"live": {}, "readings": [r("2026-10-31T10:00:00", 88)]}, f)
        s = L.Store()
        s.load()
        self.assertEqual(s.count, 1)
        self.assertTrue(os.path.exists(L.OLD_JSON_PATH + ".bak"))
        self.assertFalse(os.path.exists(L.OLD_JSON_PATH))

    def test_corrupt_old_json_does_not_stop_startup(self):
        with open(L.OLD_JSON_PATH, "w", encoding="utf-8") as f:
            f.write('{"readings": [')
        s = L.Store()
        s.load()
        self.assertEqual(s.count, 0)

    def test_live_file_carries_count(self):
        s = L.Store()
        s.load()
        s.append(r("2026-10-31T10:00:00+02:00", 50))
        s.save_live({"t": "2026-10-31T10:00:00+02:00", "state": "connected"})
        with open(L.LIVE_PATH, encoding="utf-8") as f:
            text = f.read()
        self.assertEqual(json.loads(text[text.index("{"):text.rindex("}") + 1])["count"], 1)


if __name__ == "__main__":
    unittest.main()
