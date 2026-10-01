"""Feeds added from a calendar URL: kept, fetched, and refused a writer."""
import argparse
import importlib.machinery
import importlib.util
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

sys_path = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(sys_path, "..", "..", "helper", "orchard-helper")
_loader = importlib.machinery.SourceFileLoader("orchard_helper_webcal", HELPER)
_spec = importlib.util.spec_from_loader("orchard_helper_webcal", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

FEED = "https://feeds.example.com/public.ics"
ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nX-WR-CALNAME:Public Team\r\n"
    "BEGIN:VEVENT\r\nUID:feed-1\r\nDTSTART;VALUE=DATE:20260915\r\n"
    "DTEND;VALUE=DATE:20260916\r\nSUMMARY:Public standup\r\nEND:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:feed-2\r\nDTSTART;VALUE=DATE:20260930\r\n"
    "DTEND;VALUE=DATE:20261001\r\nSUMMARY:Public retro\r\nEND:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)
CHANGED = ICS.replace("Public standup", "Public retro").replace(
    "20260915", "20260922")
EMPTY = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"


def args(command, **extra):
    base = dict(
        user="", account_id="", provider="webcal",
        server=helper.ICLOUD, calendar="", only=[], start="", end="",
        uid="", href="", rid="", at="", id=0, query="", limit=50,
        days=7, interval=0, enabled="", color="", name="", set=[],
        force=False)
    base["command"] = command
    base.update(extra)
    return argparse.Namespace(**base)


class Webcal(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "cache.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(helper.SCHEMA)
        self.conn.execute("ALTER TABLE objects ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")
        self.body = ICS.encode("utf-8")

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def login(self, url=FEED):
        with patch.object(helper, "fetch_webcal", return_value=self.body):
            return helper.dispatch(self.conn, args("login", user=url))

    def sync(self, body=None):
        data = self.body if body is None else body
        with patch.object(helper, "fetch_webcal", return_value=data):
            return helper.dispatch(self.conn, args("sync"))

    def sync_unreachable(self):
        with patch.object(helper, "fetch_webcal",
                          side_effect=helper.Failure(
                              "webcal", "could not reach the calendar (timeout)")):
            return helper.dispatch(self.conn, args("sync"))

    # ------------------------------------------------------------- adding

    def test_an_address_without_a_host_is_not_kept(self):
        with self.assertRaises(helper.Failure):
            self.login("ical.example.com")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_what_is_not_a_calendar_is_not_kept(self):
        self.body = b"Hello, not a calendar"
        with self.assertRaises(helper.Failure):
            self.login()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM accounts").fetchone()[0], 0)

    def test_a_feed_is_kept_readonly_under_its_own_name(self):
        result = self.login()
        self.assertTrue(result["ok"])
        self.assertEqual(result["provider"], "webcal")
        row = self.conn.execute(
            "SELECT user, provider, server FROM accounts").fetchone()
        self.assertEqual(row[1], "webcal")
        self.assertEqual(row[2], FEED)
        self.assertEqual(row[0], "Public Team")
        cal = self.conn.execute(
            "SELECT name, readonly, enabled FROM calendars").fetchone()
        self.assertEqual(cal[0], "Public Team")
        self.assertTrue(cal[1])
        # Nothing to put in the keyring: a credential could not exist.
        self.assertEqual(helper.stored_password(result["accountId"]), "")

    def test_new_feeds_take_distinct_colours_from_the_set(self):
        first = self.login()
        second = self.login("https://calendar.example.com/other.ics")
        colors = [c["color"] for c in first["calendars"]] + \
                 [c["color"] for c in second["calendars"]]
        for color in colors:
            self.assertIn(color, helper.PALETTE)
        # Each feed takes a colour the other feeds do not already wear, so a
        # row of them is tellable apart even before the names are read.
        self.assertEqual(len(set(colors)), len(colors))
        stored = {row[0]: row[1] for row in
                  self.conn.execute("SELECT url, color FROM calendars")}
        for color in colors:
            self.assertIn(color, set(stored.values()))
        self.assertNotIn("", set(stored.values()))

    def test_a_reconnect_keeps_the_colour_in_use(self):
        self.login()
        assigned = self.conn.execute("SELECT color FROM calendars").fetchone()[0]
        self.assertIn(assigned, helper.PALETTE)
        # A choice made later, in the app, is this one's to keep: a reconnect
        # may not reach over it.
        own = "#112233"
        self.conn.execute("UPDATE calendars SET color=?", (own,))
        self.conn.commit()
        again = self.login()
        self.assertEqual(again["calendars"][0]["color"], own)
        self.assertEqual(
            self.conn.execute("SELECT color FROM calendars").fetchone()[0], own)

    # ---------------------------------------------------------------- sync

    def test_sync_caches_events_and_skips_an_unchanged_feed(self):
        self.login()
        first = self.sync()
        self.assertTrue(first["ok"])
        cal = first["calendars"][0]
        self.assertTrue(cal["ok"])
        self.assertFalse(cal.get("skipped"))
        self.assertEqual(
            self.conn.execute(
                "SELECT summary, uid FROM objects ORDER BY uid").fetchall(),
            [("Public standup", "feed-1"), ("Public retro", "feed-2")])
        # One row per event — the layout every reader expects — not one row
        # per feed with the season inside it.
        urls = [r[0] for r in self.conn.execute(
            "SELECT url FROM objects ORDER BY url").fetchall()]
        self.assertEqual(len(urls), 2)
        self.assertNotIn(FEED, urls)
        stored_ctag = self.conn.execute(
            "SELECT ctag FROM calendars").fetchone()[0]
        self.assertTrue(stored_ctag)

        again = self.sync()
        self.assertTrue(again["calendars"][0]["skipped"])

        # The feed moves: the cached events follow it.
        moved = self.sync(CHANGED.encode("utf-8"))
        self.assertFalse(moved["calendars"][0]["skipped"])
        self.assertEqual(
            self.conn.execute(
                "SELECT summary, uid FROM objects ORDER BY uid").fetchall(),
            [("Public retro", "feed-1"), ("Public retro", "feed-2")])

    def test_a_whole_feed_cached_as_one_row_is_split(self):
        self.login()
        self.sync()
        # Pretend the feed was kept the old way: one row on the bare URL,
        # the whole feed's bytes. The digest is the same, so the skip check
        # would happily do nothing — the layout must force the rebuild.
        import hashlib
        self.conn.execute("DELETE FROM objects")
        self.conn.execute(
            "INSERT INTO objects(url,calendar,etag,ics,first_start,last_end,"
            "open_ended,summary,uid) VALUES(?,?,?,?,1,1,0,'s','feed-1')",
            (FEED, FEED, hashlib.sha256(self.body).hexdigest(),
             self.body.decode("utf-8")))
        rebuilt = self.sync()
        self.assertFalse(rebuilt["calendars"][0]["skipped"])
        urls = {r[0] for r in self.conn.execute(
            "SELECT url FROM objects").fetchall()}
        self.assertEqual(len(urls), 2)
        self.assertNotIn(FEED, urls)

    def test_a_color_picked_here_survives_refetches(self):
        self.login()
        self.sync()
        self.conn.execute(
            "UPDATE calendars SET color='#e05d5d' WHERE url=?", (FEED,))
        self.sync(EMPTY.encode("utf-8"))
        self.assertEqual(
            self.conn.execute(
                "SELECT color FROM calendars WHERE url=?", (FEED,)).fetchone()[0],
            "#e05d5d")

    def test_a_feed_that_stops_carrying_events_releases_the_cache(self):
        self.login()
        self.sync()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM objects").fetchone()[0], 2)
        cleared = self.sync(EMPTY.encode("utf-8"))
        self.assertTrue(cleared["ok"])
        self.assertEqual(cleared["calendars"][0]["removedCount"], 2)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM objects").fetchone()[0], 0)

    def test_a_failed_fetch_is_said_on_the_calendar(self):
        self.login()
        result = self.sync_unreachable()
        # The other accounts still report; the feed says its own piece.
        self.assertTrue(result["ok"])
        self.assertFalse(result["calendars"][0]["ok"])
        self.assertEqual(
            result["calendars"][0]["error"], "could not reach the calendar (timeout)")
        message = self.conn.execute(
            "SELECT error FROM calendars").fetchone()[0]
        self.assertEqual(message, "could not reach the calendar (timeout)")

    # ------------------------------------------------------------- writes

    def test_no_write_path_reaches_a_feed(self):
        self.login()
        self.sync()
        request = {"calendarUrl": FEED, "href": FEED, "uid": "feed-1"}
        with self.assertRaises(helper.Failure) as error:
            helper.write_event(self.conn, request, False)
        self.assertEqual(error.exception.code, "readonly")
        # A move toward the feed is refused too, not only a save on it.
        with self.assertRaises(helper.Failure):
            helper.write_event(
                self.conn, dict(request, targetCalendarUrl=FEED), True)
        # And nothing was cached or queued as a result.
        self.assertEqual(self.conn.execute(
            "SELECT pending FROM objects ORDER BY url").fetchall(), [(0,), (0,)])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM pending").fetchone()[0], 0)

    def test_removing_an_account_takes_the_feed_and_nothing_else(self):
        self.login()
        self.sync()
        account_id = self.conn.execute(
            "SELECT id FROM accounts").fetchone()[0]
        result = helper.dispatch(
            self.conn, argparse.Namespace(
                command="remove-account", user=account_id))
        self.assertTrue(result["ok"])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM calendars").fetchone()[0], 0)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM objects").fetchone()[0], 0)

    # --------------------------------------------------------- capabilities

    def test_capabilities_keep_a_feed_out_of_the_writable_lists(self):
        feed = helper.provider_capabilities("webcal")
        self.assertFalse(feed["create"])
        self.assertFalse(feed["write"])
        self.assertFalse(feed["contacts"])
        self.assertTrue(helper.provider_capabilities("icloud")["create"])
        self.assertFalse(helper.provider_capabilities("google")["create"])
        self.assertTrue(helper.provider_capabilities("caldav")["create"])

    def test_status_says_the_feed_is_readonly(self):
        self.login()
        calendars = helper.status(self.conn)["calendars"]
        feed = [c for c in calendars if c["url"] == FEED][0]
        self.assertTrue(feed["readonly"])
        self.assertFalse(feed["capabilities"]["create"])
        account = helper.status(self.conn)["accounts"][0]
        self.assertEqual(account["provider"], "webcal")
        self.assertFalse(account["capabilities"]["create"])


if __name__ == "__main__":
    unittest.main()
