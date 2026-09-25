"""Due calendar alerts, at the cache boundary the headless service uses."""
import importlib.machinery
import importlib.util
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "..", "helper", "omarcal-helper")
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


if __name__ == "__main__":
    unittest.main()
