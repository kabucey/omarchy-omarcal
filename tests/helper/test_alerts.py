"""Due calendar alerts, at the cache boundary the headless service uses."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "..", "helper", "omarcal-helper")
HELPER_DIR = os.path.dirname(HELPER)
LAUNCHER = os.path.join(HELPER_DIR, "omarcal")
NATIVE = os.path.join(HELPER_DIR, "omarcal-native")
_loader = importlib.machinery.SourceFileLoader("omarcal_helper_alerts", HELPER)
_spec = importlib.util.spec_from_loader("omarcal_helper_alerts", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)


SERIES = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0",
    "BEGIN:VEVENT", "UID:series-alert", "DTSTART:20260922T140000Z",
    "DTEND:20260922T150000Z", "RRULE:FREQ=DAILY;COUNT=2", "SUMMARY:Class",
    "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT15M", "END:VALARM",
    # Provider placeholder: preserved, but never due in a current window.
    "BEGIN:VALARM", "ACTION:AUDIO", "TRIGGER;VALUE=DATE-TIME:19760401T005545Z",
    "END:VALARM", "END:VEVENT",
    "BEGIN:VEVENT", "UID:series-alert", "RECURRENCE-ID:20260923T140000Z",
    "DTSTART:20260923T160000Z", "DTEND:20260923T170000Z", "SUMMARY:Class moved",
    "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT5M", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR", "",
])

END_RELATED = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:end-alert",
    "DTSTART:20260922T140000Z", "DTEND:20260922T150000Z", "SUMMARY:Office hours",
    "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER;RELATED=END:-PT10M", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR", "",
])

AT_EVENT = "\r\n".join([
    "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", "UID:zero-alert",
    "DTSTART:20260922T140000Z", "DTEND:20260922T150000Z", "SUMMARY:Standup",
    # Google commonly serializes an at-event reminder in this form.
    "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:P0D", "END:VALARM",
    "END:VEVENT", "END:VCALENDAR", "",
])


class Alerts(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.conn = sqlite3.connect(os.path.join(self.folder.name, "c.db"))
        self.conn.executescript(helper.SCHEMA)
        self.conn.execute(
            "INSERT INTO calendars(url,name,color,enabled) VALUES(?,?,?,1)",
            ("https://x/cal/", "Work", "#44aaff"),
        )

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def add(self, href, ics, calendar="https://x/cal/"):
        first, last, open_ended, summary, uid = helper.index_of(ics)
        self.conn.execute(
            "INSERT INTO objects(url,calendar,ics,first_start,last_end,open_ended,summary,uid) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (href, calendar, ics, first, last, open_ended, summary, uid),
        )

    def test_recurring_master_and_override_fire_once_at_their_own_offsets(self):
        self.add("https://x/cal/series.ics", SERIES)
        result = helper.alerts(
            self.conn,
            datetime(2026, 9, 22, 13, 44, tzinfo=timezone.utc),
            datetime(2026, 9, 23, 15, 56, tzinfo=timezone.utc),
        )

        self.assertEqual([item["title"] for item in result["alerts"]],
                         ["Class", "Class moved"])
        self.assertEqual([item["at"] for item in result["alerts"]],
                         ["2026-09-22T13:45:00Z", "2026-09-23T15:55:00Z"])
        self.assertEqual(len({item["id"] for item in result["alerts"]}), 2)

    def test_related_end_uses_the_occurrence_end(self):
        self.add("https://x/cal/end.ics", END_RELATED)
        result = helper.alerts(
            self.conn,
            datetime(2026, 9, 22, 14, 49, tzinfo=timezone.utc),
            datetime(2026, 9, 22, 14, 51, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(result["alerts"][0]["at"], "2026-09-22T14:50:00Z")

    def test_google_zero_duration_fires_at_event_time(self):
        self.add("https://x/cal/zero.ics", AT_EVENT)
        result = helper.alerts(
            self.conn,
            datetime(2026, 9, 22, 13, 59, tzinfo=timezone.utc),
            datetime(2026, 9, 22, 14, 1, tzinfo=timezone.utc),
        )
        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(result["alerts"][0]["at"], "2026-09-22T14:00:00Z")

    def test_disabled_calendar_does_not_notify(self):
        self.conn.execute(
            "INSERT INTO calendars(url,name,enabled) VALUES('https://x/off/','Off',0)")
        self.add("https://x/off/end.ics", END_RELATED, "https://x/off/")
        result = helper.alerts(
            self.conn,
            datetime(2026, 9, 22, 14, 49, tzinfo=timezone.utc),
            datetime(2026, 9, 22, 14, 51, tzinfo=timezone.utc),
        )
        self.assertEqual(result["alerts"], [])


def command_event(uid, title, start):
    end = start + timedelta(hours=1)
    return "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "BEGIN:VEVENT", f"UID:{uid}",
        f"DTSTART:{start.strftime('%Y%m%dT%H%M%SZ')}",
        f"DTEND:{end.strftime('%Y%m%dT%H%M%SZ')}", f"SUMMARY:{title}",
        "BEGIN:VALARM", "ACTION:DISPLAY", "TRIGGER:-PT5M", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", "",
    ])


class AlertCacheAndBackend(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state_home = os.path.join(self.folder.name, "state")
        cache_dir = os.path.join(self.state_home, "omarcal")
        os.makedirs(cache_dir)
        self.conn = sqlite3.connect(os.path.join(cache_dir, "cache.db"))
        self.conn.executescript(helper.SCHEMA)
        self.conn.execute(
            "INSERT INTO calendars(url,name,enabled) VALUES(?,?,1)",
            ("https://x/cache/", "Cache"),
        )

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def add(self, href, ics):
        first, last, open_ended, summary, uid = helper.index_of(ics)
        self.conn.execute(
            "INSERT INTO objects(url,calendar,ics,first_start,last_end,open_ended,summary,uid) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (href, "https://x/cache/", ics, first, last, open_ended, summary, uid),
        )

    def run_alert_command(self, executable, start, end, state_home=None):
        env = os.environ.copy()
        env["XDG_STATE_HOME"] = state_home or self.state_home
        completed = subprocess.run(
            [executable, "alerts", "--from", start.isoformat(timespec="seconds"),
             "--to", end.isoformat(timespec="seconds")],
            capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)["alerts"]

    def create_cached_alert(self):
        # The range moves with the test clock and remains inside the bounded
        # projection window no matter when this suite runs.
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now + timedelta(minutes=20)
        end = start + timedelta(hours=2)
        href = "https://x/cache/event.ics"
        ics = command_event("cache-event", "Before change", start)
        self.add(href, ics)
        self.conn.commit()
        expected = helper.alerts(self.conn, start - timedelta(minutes=7),
                                 start + timedelta(hours=2))
        self.assertEqual([item["title"] for item in expected["alerts"]], ["Before change"])
        self.conn.commit()
        return href, start, end

    def test_alert_results_refresh_after_an_object_changes(self):
        href, start, end = self.create_cached_alert()
        changed_start = start + timedelta(hours=1)
        changed_ics = command_event("cache-event", "After change", changed_start)
        first, last, open_ended, summary, uid = helper.index_of(changed_ics)

        # This is the same persisted object state sync replaces. The next
        # public alert read must discard its old projected occurrence.
        self.conn.execute(
            "UPDATE objects SET ics=?,first_start=?,last_end=?,open_ended=?,summary=?,uid=? "
            "WHERE url=?",
            (changed_ics, first, last, open_ended, summary, uid, href),
        )
        self.conn.commit()
        refreshed = helper.alerts(self.conn, start - timedelta(minutes=7), end)

        self.assertEqual(
            [(item["title"], item["at"]) for item in refreshed["alerts"]],
            [("After change", (changed_start - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))],
        )

    def test_launcher_falls_back_when_no_native_binary_is_installed(self):
        href = "https://x/cache/event.ics"
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = now + timedelta(minutes=20)
        end = start + timedelta(minutes=1)
        self.add(href, command_event("fallback-event", "Fallback", start))
        self.conn.commit()

        # Exercise the shipped launcher in an install-shaped directory that
        # intentionally has no native executable beside it.
        install = os.path.join(self.folder.name, "plugin", "helper")
        os.makedirs(install)
        launcher = os.path.join(install, "omarcal")
        shutil.copy2(LAUNCHER, launcher)
        shutil.copy2(HELPER, os.path.join(install, "omarcal-helper"))
        alerts = self.run_alert_command(launcher, start - timedelta(minutes=7), end)
        self.assertEqual([item["title"] for item in alerts], ["Fallback"])
        self.assertEqual(alerts[0]["at"],
                         (start - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    @unittest.skipUnless(os.path.isfile(NATIVE) and os.access(NATIVE, os.X_OK),
                         "native helper is not built")
    def test_native_reads_a_nonempty_projection_without_python(self):
        _href, start, _end = self.create_cached_alert()
        range_start = start - timedelta(minutes=7)
        range_end = start + timedelta(minutes=2)
        self.conn.close()

        # No Python helper is present beside the launcher, so this nonempty
        # response proves that the executable served the cached alert.
        install = os.path.join(self.folder.name, "native-only", "helper")
        os.makedirs(install)
        launcher = os.path.join(install, "omarcal")
        shutil.copy2(LAUNCHER, launcher)
        shutil.copy2(NATIVE, os.path.join(install, "omarcal-native"))
        alerts = self.run_alert_command(launcher, range_start, range_end)
        self.assertEqual([item["title"] for item in alerts], ["Before change"])
        self.assertEqual(alerts[0]["at"],
                         (start - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    @unittest.skipUnless(os.path.isfile(NATIVE) and os.access(NATIVE, os.X_OK),
                         "native helper is not built")
    def test_native_alerts_match_python_alerts(self):
        _href, start, _end = self.create_cached_alert()
        # Use a range that includes the expected reminder and the projection
        # builder's surrounding coverage.
        range_start = start - timedelta(minutes=7)
        range_end = start + timedelta(minutes=2)
        self.conn.close()
        env = os.environ.copy()
        env["XDG_STATE_HOME"] = self.state_home
        command = ["alerts", "--from", range_start.isoformat(timespec="seconds"),
                   "--to", range_end.isoformat(timespec="seconds")]
        python_result = subprocess.run(
            [sys.executable, HELPER, *command], capture_output=True, text=True, env=env,
            check=False,
        )
        native_result = subprocess.run(
            [LAUNCHER, *command], capture_output=True, text=True, env=env, check=False,
        )
        self.assertEqual(python_result.returncode, 0, python_result.stderr or python_result.stdout)
        self.assertEqual(native_result.returncode, 0, native_result.stderr or native_result.stdout)
        self.assertEqual(json.loads(native_result.stdout)["alerts"],
                         json.loads(python_result.stdout)["alerts"])


if __name__ == "__main__":
    unittest.main()
