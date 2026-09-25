"""Calendar sync's transport and checkpoint contracts, against a local fake DAV."""
import importlib.machinery
import importlib.util
import multiprocessing
import os
import sqlite3
import sys
import threading
import time
import types
import unittest
from xml.etree import ElementTree as ET

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "..", "helper", "omarcal-helper")

# Sync itself does not need libical. Load the production helper with a tiny
# import-only stub so these transport tests can run when ICalGLib is absent;
# index_of is replaced below, and no calendar parsing is claimed here.
fake_gi = types.ModuleType("gi")
fake_gi.require_version = lambda *_args: None
fake_repository = types.ModuleType("gi.repository")
fake_ical = types.ModuleType("ICalGLib")
fake_ical.Unknowntokenhandling = types.SimpleNamespace(ASSUME_IANA_TOKEN=0)
fake_ical.set_unknown_token_handling_setting = lambda *_args: None
fake_repository.ICalGLib = fake_ical
saved_modules = {name: sys.modules.get(name) for name in ("gi", "gi.repository")}
sys.modules["gi"] = fake_gi
sys.modules["gi.repository"] = fake_repository
try:
    _loader = importlib.machinery.SourceFileLoader("omarcal_helper_sync", HELPER)
    _spec = importlib.util.spec_from_loader("omarcal_helper_sync", _loader)
    helper = importlib.util.module_from_spec(_spec)
    _loader.exec_module(helper)
finally:
    for name, old in saved_modules.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old

REAL_INDEX_OF = helper.index_of


def report_xml(entries, token="next-token"):
    root = ET.Element("{DAV:}multistatus")
    ET.SubElement(root, "{DAV:}sync-token").text = token
    for href, etag, status in entries:
        response = ET.SubElement(root, "{DAV:}response")
        ET.SubElement(response, "{DAV:}href").text = href
        if status:
            ET.SubElement(response, "{DAV:}status").text = f"HTTP/1.1 {status} test"
        else:
            propstat = ET.SubElement(response, "{DAV:}propstat")
            prop = ET.SubElement(propstat, "{DAV:}prop")
            ET.SubElement(prop, "{DAV:}getetag").text = etag
            ET.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
    return ET.tostring(root)


def multiget_xml(entries):
    root = ET.Element("{DAV:}multistatus")
    for href, etag, data in entries:
        response = ET.SubElement(root, "{DAV:}response")
        ET.SubElement(response, "{DAV:}href").text = href
        propstat = ET.SubElement(response, "{DAV:}propstat")
        prop = ET.SubElement(propstat, "{DAV:}prop")
        ET.SubElement(prop, "{DAV:}getetag").text = etag
        ET.SubElement(prop, "{urn:ietf:params:xml:ns:caldav}calendar-data").text = data
        ET.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
    return ET.tostring(root)


EVENT = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:test\r\nEND:VEVENT\r\nEND:VCALENDAR"


class FakeSession:
    def __init__(self, reports, multi_mode="complete", report_statuses=None):
        self.reports = reports
        self.multi_mode = multi_mode
        self.report_statuses = list(report_statuses or [])
        self.multi_attempts = 0
        self.report_calls = 0
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def request(self, method, url, body="", depth="0"):
        if "calendar-multiget" not in body:
            self.report_calls += 1
            status = self.report_statuses.pop(0) if self.report_statuses else 207
            report = self.reports[url]
            if isinstance(report, list):
                report = report.pop(0)
            return status, report if status in (200, 207) else b""
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(0.015)
            self.multi_attempts += 1
            if self.multi_mode == "http500":
                return 500, b""
            requested = [el.text for el in ET.fromstring(body).findall("d:href", helper.NS)]
            if self.multi_mode == "missing":
                root = ET.Element("{DAV:}multistatus")
                missing = ET.SubElement(root, "{DAV:}response")
                ET.SubElement(missing, "{DAV:}href").text = requested[0]
                ET.SubElement(missing, "{DAV:}status").text = "HTTP/1.1 404 Not Found"
                present = [(href, '"new"', EVENT) for href in requested[1:]]
                for response in ET.fromstring(multiget_xml(present)):
                    root.append(response)
                return 207, ET.tostring(root)
            entries = [(href, '"new"', EVENT) for href in requested]
            if self.multi_mode == "partial" and entries:
                entries = entries[:-1]
            return 207, multiget_xml(entries)
        finally:
            with self.lock:
                self.active -= 1

    def close(self):
        pass


def calendar(index, ctag="new-ctag"):
    return {"url": f"https://dav.example.test/cal{index}/", "href": f"/cal{index}/",
            "name": f"Calendar {index}", "color": "", "ctag": ctag,
            "readonly": False}


def sync_child(result_queue):
    conn = sqlite3.connect(":memory:")
    conn.executescript(helper.SCHEMA)
    calendars = [calendar(i) for i in range(4)]
    reports = {
        cal["url"]: report_xml([(f"/cal{i}/event.ics", '"old"', "")], f"token-{i}")
        for i, cal in enumerate(calendars)
    }
    fake = FakeSession(reports)
    helper.load_password = lambda _account: "password"
    helper.DavSession = lambda *_args: fake
    helper.discover = lambda *_args: {"calendars": calendars}
    helper.index_of = lambda _ics: (0, 0, 1, "", "test")
    try:
        results = helper.sync_account(conn, "person", "https://dav.example.test/", "", False)
        result_queue.put({"ok": len(results) == 4 and all(r["ok"] for r in results),
                          "max_active": fake.max_active,
                          "objects": conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0]})
    except Exception as error:  # sent to the parent so a child failure is visible
        result_queue.put({"error": str(error)})
    finally:
        conn.close()


class SyncBoundary(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(helper.SCHEMA)
        self.cal = calendar(0)
        self.saved = (helper.load_password, helper.DavSession, helper.discover, helper.index_of)
        helper.load_password = lambda _account: "password"
        helper.index_of = lambda _ics: (0, 0, 1, "", "test")
        self.session = None

    def tearDown(self):
        helper.load_password, helper.DavSession, helper.discover, helper.index_of = self.saved
        self.conn.close()

    def use_session(self, reports, multi_mode="complete", report_statuses=None):
        self.session = FakeSession(reports, multi_mode, report_statuses)
        helper.DavSession = lambda *_args: self.session
        helper.discover = lambda *_args: {"calendars": [self.cal.copy()]}
        return self.session

    def run_sync(self):
        return helper.sync_account(self.conn, "person", "https://dav.example.test/", "", False)

    def test_four_calendar_syncs_finish_with_bounded_parallel_requests(self):
        context = multiprocessing.get_context("fork")
        result_queue = context.Queue()
        process = context.Process(target=sync_child, args=(result_queue,))
        process.start()
        process.join(4)
        completed = not process.is_alive()
        if not completed:
            process.terminate()
            process.join()
        self.assertTrue(completed, "four calendars must not wait on their own pool")
        self.assertEqual(process.exitcode, 0)
        result = result_queue.get(timeout=1)
        self.assertNotIn("error", result)
        self.assertTrue(result["ok"])
        self.assertEqual(result["objects"], 4)
        self.assertLessEqual(result["max_active"], helper.SYNC_WORKERS)
        self.assertGreaterEqual(result["max_active"], 2)

    def test_failed_or_partial_multiget_preserves_cached_rows_and_checkpoint(self):
        self.conn.execute(
            "INSERT INTO calendars(url,href,name,ctag,token,account) VALUES(?,?,?,?,?,?)",
            (self.cal["url"], self.cal["href"], self.cal["name"], "old-ctag", "old-token", "person"))
        deleted = self.cal["url"] + "gone.ics"
        changed = self.cal["url"] + "changed.ics"
        self.conn.executemany(
            "INSERT INTO objects(url,calendar,etag,ics,uid) VALUES(?,?,?,?,?)",
            [(deleted, self.cal["url"], '"gone"', "old gone", "gone"),
             (changed, self.cal["url"], '"old"', "old changed", "changed")])
        report = report_xml([
            ("/cal0/gone.ics", "", 404),
            ("/cal0/changed.ics", '"new"', ""),
            ("/cal0/another.ics", '"new"', ""),
        ], "uncommitted-token")

        for mode in ("http500", "partial"):
            with self.subTest(mode=mode):
                self.conn.execute("UPDATE calendars SET ctag='old-ctag', token='old-token' WHERE url=?",
                                  (self.cal["url"],))
                self.use_session({self.cal["url"]: report}, mode)
                (outcome,) = self.run_sync()
                self.assertFalse(outcome["ok"])
                rows = self.conn.execute("SELECT url,ics FROM objects ORDER BY url").fetchall()
                self.assertEqual(rows, [(changed, "old changed"), (deleted, "old gone")])
                checkpoint = self.conn.execute(
                    "SELECT ctag,token FROM calendars WHERE url=?", (self.cal["url"],)).fetchone()
                self.assertEqual(checkpoint, ("old-ctag", "old-token"))

    def test_first_sync_failure_does_not_seed_ctag_and_next_sync_fetches(self):
        report = report_xml([("/cal0/new.ics", '"v1"', "")], "first-token")
        session = self.use_session({self.cal["url"]: report}, "http500")
        (failed,) = self.run_sync()
        self.assertFalse(failed["ok"])
        self.assertEqual(self.conn.execute(
            "SELECT ctag,token FROM calendars WHERE url=?", (self.cal["url"],)).fetchone(), ("", ""))

        session.multi_mode = "complete"
        (retried,) = self.run_sync()
        self.assertTrue(retried["ok"])
        self.assertFalse(retried.get("skipped", False))
        self.assertEqual(session.report_calls, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 1)
        self.assertEqual(self.conn.execute(
            "SELECT ctag,token FROM calendars WHERE url=?", (self.cal["url"],)).fetchone(),
            ("new-ctag", "first-token"))

    def test_malformed_or_non_vevent_data_does_not_advance_checkpoint(self):
        report = report_xml([("/cal0/not-an-event.ics", '"v1"', "")], "bad-data-token")
        session = self.use_session({self.cal["url"]: report})
        fake_ical.Component = types.SimpleNamespace(new_from_string=lambda _ics: None)
        helper.index_of = REAL_INDEX_OF
        (outcome,) = self.run_sync()
        self.assertFalse(outcome["ok"])
        self.assertIn("contained no VEVENT", outcome["error"])
        self.assertEqual(self.conn.execute(
            "SELECT ctag,token FROM calendars WHERE url=?", (self.cal["url"],)).fetchone(), ("", ""))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM objects").fetchone()[0], 0)

    def test_full_sync_removes_cached_objects_absent_from_complete_listing(self):
        self.conn.execute(
            "INSERT INTO calendars(url,href,name,ctag,token,account) VALUES(?,?,?,?,?,?)",
            (self.cal["url"], self.cal["href"], self.cal["name"], "", "", "person"))
        stale = self.cal["url"] + "no-longer-listed.ics"
        self.conn.execute("INSERT INTO objects(url,calendar,ics) VALUES(?,?,?)",
                          (stale, self.cal["url"], "old"))
        report = report_xml([("/cal0/current.ics", '"v1"', "")], "full-token")
        self.use_session({self.cal["url"]: report})
        (outcome,) = self.run_sync()
        self.assertTrue(outcome["ok"])
        self.assertEqual(self.conn.execute("SELECT url FROM objects").fetchall(),
                         [(self.cal["url"] + "current.ics",)])

    def test_stale_token_fallback_reconciles_absent_cached_objects(self):
        self.conn.execute(
            "INSERT INTO calendars(url,href,name,ctag,token,account) VALUES(?,?,?,?,?,?)",
            (self.cal["url"], self.cal["href"], self.cal["name"], "old-ctag", "stale-token", "person"))
        absent = self.cal["url"] + "absent.ics"
        self.conn.execute("INSERT INTO objects(url,calendar,ics) VALUES(?,?,?)",
                          (absent, self.cal["url"], "old"))
        self.cal["ctag"] = "new-ctag"
        report = report_xml([("/cal0/current.ics", '"v1"', "")], "recovered-token")
        session = self.use_session({self.cal["url"]: report}, report_statuses=[409, 207])
        (outcome,) = self.run_sync()
        self.assertTrue(outcome["ok"])
        self.assertEqual(session.report_calls, 2)
        self.assertEqual(self.conn.execute("SELECT url FROM objects").fetchall(),
                         [(self.cal["url"] + "current.ics",)])
        self.assertEqual(self.conn.execute(
            "SELECT ctag,token FROM calendars WHERE url=?", (self.cal["url"],)).fetchone(),
            ("new-ctag", "recovered-token"))

    def test_limited_sync_report_follows_continuation_token_before_committing(self):
        first = report_xml([
            ("/cal0/first.ics", '"v1"', ""),
            ("/cal0/", "", 507),
        ], "page-one-token")
        second = report_xml([
            ("/cal0/second.ics", '"v1"', ""),
        ], "complete-token")
        session = self.use_session({self.cal["url"]: [first, second]})

        (outcome,) = self.run_sync()

        self.assertTrue(outcome["ok"])
        self.assertEqual(session.report_calls, 2)
        self.assertEqual(self.conn.execute(
            "SELECT url FROM objects ORDER BY url").fetchall(), [
                (self.cal["url"] + "first.ics",),
                (self.cal["url"] + "second.ics",),
            ])
        self.assertEqual(self.conn.execute(
            "SELECT token FROM calendars WHERE url=?", (self.cal["url"],)).fetchone()[0],
            "complete-token")

    def test_event_removed_between_report_and_multiget_does_not_fail_sync(self):
        vanished = self.cal["url"] + "vanished.ics"
        present = self.cal["url"] + "present.ics"
        self.conn.execute("INSERT INTO objects(url,calendar,etag,ics,uid) VALUES(?,?,?,?,?)",
                          (vanished, self.cal["url"], '"old"', "old", "vanished"))
        report = report_xml([
            ("/cal0/vanished.ics", '"changed"', ""),
            ("/cal0/present.ics", '"new"', ""),
        ])
        self.use_session({self.cal["url"]: report}, "missing")

        (outcome,) = self.run_sync()

        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["removedCount"], 1)
        self.assertEqual(self.conn.execute(
            "SELECT url FROM objects ORDER BY url").fetchall(), [(present,)])


class DavOriginBoundary(unittest.TestCase):
    def test_absolute_href_is_same_origin_or_an_explicit_icloud_shard(self):
        base = "https://caldav.icloud.com/principal/"
        self.assertEqual(helper.absolute(base, "/home/cal/"), "https://caldav.icloud.com/home/cal/")
        self.assertEqual(helper.absolute(base, "https://p123-caldav.icloud.com/home/cal/"),
                         "https://p123-caldav.icloud.com/home/cal/")
        for href in ("https://attacker.invalid/collect", "//attacker.invalid/collect",
                     "http://caldav.icloud.com/downgrade"):
            with self.subTest(href=href), self.assertRaises(helper.Failure):
                helper.absolute(base, href)

    def test_dav_urls_reject_controls_and_backslashes_before_connection(self):
        attempts = []

        class Connection:
            def __init__(self, host, **_kwargs):
                attempts.append(host)

        saved = helper.http.client.HTTPSConnection
        helper.http.client.HTTPSConnection = Connection
        try:
            base = "https://dav.example.test/"
            session = helper.DavSession("person", "secret", base)
            for bad in ("/cal/with\nnewline.ics", "/cal/with\\slash.ics", "/cal/with\x7fdelete.ics"):
                with self.subTest(href=bad), self.assertRaises(helper.Failure):
                    helper.absolute(base, bad)
                with self.subTest(url=bad), self.assertRaises(helper.Failure):
                    session.request("PROPFIND", base + bad.lstrip("/"))
            session.close()
        finally:
            helper.http.client.HTTPSConnection = saved
        self.assertEqual(attempts, [])

    def test_redirect_to_untrusted_host_is_refused_before_credentials_are_sent(self):
        requests = []

        class Response:
            status = 302

            def read(self, _limit):
                return b""

            def getheader(self, name):
                return "https://attacker.invalid/collect" if name == "Location" else None

        class Connection:
            def __init__(self, host, **_kwargs):
                self.host = host

            def request(self, method, path, body=None, headers=None):
                requests.append((self.host, method, path, headers))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        saved = helper.http.client.HTTPSConnection
        helper.http.client.HTTPSConnection = Connection
        try:
            session = helper.DavSession("person", "secret", "https://dav.example.test/")
            with self.assertRaises(helper.Failure):
                session.request("PROPFIND", "https://dav.example.test/")
            with self.assertRaises(helper.Failure):
                session.request("PROPFIND", "https://attacker.invalid/collect")
            session.close()
        finally:
            helper.http.client.HTTPSConnection = saved
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], "dav.example.test")
        self.assertIn("Authorization", requests[0][3])

    def test_icloud_calendar_shard_redirect_remains_usable(self):
        requests = []

        class Response:
            def __init__(self, status, location=""):
                self.status = status
                self.location = location

            def read(self, _limit):
                return b""

            def getheader(self, name):
                return self.location if name == "Location" else None

        class Connection:
            def __init__(self, host, **_kwargs):
                self.host = host

            def request(self, method, path, body=None, headers=None):
                requests.append((self.host, headers))

            def getresponse(self):
                if self.host == "caldav.icloud.com":
                    return Response(301, "https://p123-caldav.icloud.com/home/")
                return Response(207)

            def close(self):
                pass

        saved = helper.http.client.HTTPSConnection
        helper.http.client.HTTPSConnection = Connection
        try:
            session = helper.DavSession("person", "secret", helper.ICLOUD)
            self.assertEqual(session.request("PROPFIND", helper.ICLOUD)[0], 207)
            session.close()
        finally:
            helper.http.client.HTTPSConnection = saved
        self.assertEqual([host for host, _headers in requests],
                         ["caldav.icloud.com", "p123-caldav.icloud.com"])
        self.assertTrue(all("Authorization" in headers for _host, headers in requests))


class DavResponseLimit(unittest.TestCase):
    def test_both_dav_read_paths_reject_oversize_and_drop_the_connection(self):
        class Response:
            status = 200

            def __init__(self):
                self.read_limit = 0

            def read(self, limit):
                self.read_limit = limit
                return b"12345"[:limit]

            def getheader(self, _name):
                return None

            def getheaders(self):
                return []

        class Connection:
            def __init__(self, host, response, **_kwargs):
                self.host = host
                self.response = response
                self.closed = False

            def request(self, *_args, **_kwargs):
                pass

            def getresponse(self):
                return self.response

            def close(self):
                self.closed = True

        saved_connection = helper.http.client.HTTPSConnection
        saved_limit = helper.MAX_RESPONSE_BYTES
        try:
            helper.MAX_RESPONSE_BYTES = 4
            for reader in ("request", "send"):
                with self.subTest(reader=reader):
                    response = Response()
                    connection = Connection("dav.example.test", response)
                    helper.http.client.HTTPSConnection = lambda _host, **_kwargs: connection
                    session = helper.DavSession("person", "secret", "https://dav.example.test/")
                    with self.assertRaises(helper.Failure):
                        if reader == "request":
                            session.request("GET", "https://dav.example.test/event.ics")
                        else:
                            helper.send(session, "GET", "https://dav.example.test/event.ics", None, {})
                    self.assertEqual(response.read_limit, helper.MAX_RESPONSE_BYTES + 1)
                    self.assertTrue(connection.closed)
                    self.assertEqual(session._local.pool, {})
        finally:
            helper.http.client.HTTPSConnection = saved_connection
            helper.MAX_RESPONSE_BYTES = saved_limit


class WritePreconditions(unittest.TestCase):
    def test_updates_and_deletes_require_if_match_but_creates_use_if_none_match(self):
        sent = []

        class Response:
            status = 201

            def getheaders(self):
                return [("ETag", '"written"')]

            def read(self, _limit):
                return b""

        class Connection:
            def __init__(self, host, **_kwargs):
                self.host = host

            def request(self, method, path, body=None, headers=None):
                sent.append((method, path, body, headers))

            def getresponse(self):
                return Response()

            def close(self):
                pass

        saved = helper.http.client.HTTPSConnection
        helper.http.client.HTTPSConnection = Connection
        try:
            session = helper.DavSession("person", "secret", "https://dav.example.test/")
            for op in (
                {"method": "PUT", "url": "https://dav.example.test/event.ics", "body": "x"},
                {"method": "DELETE", "url": "https://dav.example.test/event.ics"},
            ):
                with self.subTest(op=op), self.assertRaises(helper.Failure):
                    helper.run_writes(session, [op])
            self.assertEqual(sent, [])

            helper.run_writes(session, [
                {"method": "PUT", "url": "https://dav.example.test/new.ics", "body": "x",
                 "ifNoneMatch": True},
                {"method": "PUT", "url": "https://dav.example.test/event.ics", "body": "x",
                 "ifMatch": '"old"'},
                {"method": "DELETE", "url": "https://dav.example.test/event.ics",
                 "ifMatch": '"old"'},
            ])
            session.close()
        finally:
            helper.http.client.HTTPSConnection = saved
        self.assertEqual([headers.get("If-None-Match") for _method, _path, _body, headers in sent],
                         ["*", None, None])
        self.assertEqual([headers.get("If-Match") for _method, _path, _body, headers in sent],
                         [None, '"old"', '"old"'])


if __name__ == "__main__":
    unittest.main()
