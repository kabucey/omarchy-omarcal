"""Google OAuth, provider identity, ownership, and authenticated DAV contracts."""
import base64
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import patch

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(HERE, "..", "..", "helper", "omarcal-helper")

# These tests target OAuth/transport/schema behavior and do not parse iCalendar.
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
    _loader = importlib.machinery.SourceFileLoader("omarcal_helper_google", HELPER)
    _spec = importlib.util.spec_from_loader("omarcal_helper_google", _loader)
    helper = importlib.util.module_from_spec(_spec)
    _loader.exec_module(helper)
finally:
    for name, old in saved_modules.items():
        if old is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = old


class SchemaMigration(unittest.TestCase):
    def test_legacy_email_ids_still_own_events_and_queued_changes(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            "CREATE TABLE accounts (user TEXT PRIMARY KEY, server TEXT NOT NULL, "
            "home TEXT NOT NULL DEFAULT '', added INTEGER NOT NULL DEFAULT 0)"
        )
        conn.executescript(helper.SCHEMA)
        user = "Owner@example.test"
        conn.execute("INSERT INTO accounts(user,server,home,added) VALUES(?,?,?,?)",
                     (user, helper.ICLOUD, "https://caldav.icloud.com/home/", 7))
        calendar = "https://caldav.icloud.com/home/cal/"
        event_url = calendar + "event.ics"
        conn.execute("INSERT INTO calendars(url,account) VALUES(?,?)", (calendar, user))
        conn.execute("INSERT INTO objects(url,calendar,ics,uid) VALUES(?,?,?,?)",
                     (event_url, calendar, "ics", "legacy-event"))
        pending_id = conn.execute(
            "INSERT INTO pending(action,request) VALUES('save','{}')").lastrowid
        conn.execute("INSERT INTO pending_backup(pending_id,url,existed,calendar) "
                     "VALUES(?,?,1,?)", (pending_id, event_url, calendar))
        conn.execute("INSERT INTO contacts(account,name,email) VALUES(?,?,?)",
                     (user, "Sam", "sam@example.test"))
        conn.execute("INSERT INTO contact_state(account,synced_at) VALUES(?,7)", (user,))
        conn.commit()

        helper.migrate_accounts(conn)

        account = helper.account_record(conn, user)
        self.assertEqual((account["id"], account["user"], account["identity"], account["provider"]),
                         (user, user, user.casefold(), "icloud"))
        self.assertEqual(helper.resolve_account_id(conn, user, "icloud"), user)
        self.assertEqual(conn.execute("SELECT account FROM calendars").fetchone()[0], user)
        self.assertEqual(conn.execute("SELECT uid FROM objects").fetchone()[0], "legacy-event")
        self.assertEqual(conn.execute("SELECT pending_id FROM pending_backup").fetchone()[0],
                         pending_id)
        self.assertEqual(conn.execute("SELECT account FROM contacts").fetchone()[0], user)
        self.assertEqual(conn.execute("SELECT account FROM contact_state").fetchone()[0], user)
        conn.close()


class GoogleOAuthContracts(unittest.TestCase):
    def test_browser_oauth_requires_fixed_callback_and_uses_pkce_and_verified_userinfo(self):
        exchange = []
        userinfo_calls = []
        callback_errors = []

        class FakeCallbackServer:
            def __init__(self, _address, state):
                self.expected_state = state
                self.server_address = ("127.0.0.1", 32123)
                self.result = None
                self.calls = 0

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def handle_request(self):
                self.calls += 1
                query = urlencode({"state": self.expected_state,
                                   "code": "one-time-code" if self.calls > 1 else "wrong-path"})
                path = ("/wrong?" if self.calls == 1 else helper.GOOGLE_CALLBACK_PATH + "?") + query
                handler = object.__new__(helper._OAuthCallbackHandler)
                handler.path = path
                handler.server = self
                handler.wfile = io.BytesIO()
                handler.send_error = lambda code, *_args: callback_errors.append(code)
                handler.send_response = lambda *_args: None
                handler.send_header = lambda *_args: None
                handler.end_headers = lambda: None
                helper._OAuthCallbackHandler.do_GET(handler)

        def fake_browser_open(url, **_options):
            query = parse_qs(urlsplit(url).query)
            redirect = urlsplit(query["redirect_uri"][0])
            self.assertEqual(urlsplit(url).hostname, helper.GOOGLE_AUTH_HOST)
            self.assertEqual(query["scope"], [helper.GOOGLE_SCOPES])
            self.assertIn("openid", query["scope"][0].split())
            self.assertIn("email", query["scope"][0].split())
            self.assertEqual(query["code_challenge_method"], ["S256"])
            self.assertEqual(redirect.path, helper.GOOGLE_CALLBACK_PATH)
            self.auth_query = query
            return True

        def token_exchange(fields):
            exchange.append(fields.copy())
            if fields.get("grant_type") == "authorization_code":
                return {"access_token": "short-lived", "refresh_token": "refresh-secret",
                        "expires_in": 3600, "scope": helper.GOOGLE_SCOPES}
            if fields.get("grant_type") == "refresh_token":
                return {"access_token": "fresh-access", "expires_in": 3600}
            raise AssertionError("unexpected token grant")

        def https_request(host, path, **kwargs):
            userinfo_calls.append((host, path, kwargs.get("headers", {})))
            if len(userinfo_calls) == 1:
                return 401, {}, b"{}"
            return 200, {}, json.dumps({"sub": "stable-google-sub", "email": "new@example.test",
                                        "email_verified": True}).encode()

        with patch.object(helper.webbrowser, "open", side_effect=fake_browser_open), \
             patch.object(helper, "_OAuthCallbackServer", FakeCallbackServer), \
             patch.object(helper, "google_token_exchange", side_effect=token_exchange), \
             patch.object(helper, "google_https_request", side_effect=https_request):
            auth, identity = helper.google_authorize("", {"client_id": "desktop.apps.googleusercontent.com",
                                                          "client_secret": "client-secret"})

        self.assertEqual(callback_errors, [404], "callbacks outside the fixed path are rejected")
        self.assertEqual(identity["sub"], "stable-google-sub")
        self.assertEqual([call[0:2] for call in userinfo_calls], [
            (helper.GOOGLE_IDENTITY_HOST, helper.GOOGLE_USERINFO_PATH),
            (helper.GOOGLE_IDENTITY_HOST, helper.GOOGLE_USERINFO_PATH),
        ])
        self.assertEqual([call[2]["Authorization"] for call in userinfo_calls],
                         ["Bearer short-lived", "Bearer fresh-access"])
        fields = exchange[0]
        self.assertEqual(fields["code"], "one-time-code")
        self.assertEqual(fields["redirect_uri"], self.auth_query["redirect_uri"][0])
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(fields["code_verifier"].encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(self.auth_query["code_challenge"], [expected])
        self.assertNotIn("login_hint", self.auth_query)
        self.assertEqual(auth.refresh_token, "refresh-secret")
        self.assertEqual(exchange[1]["grant_type"], "refresh_token")

    def test_same_email_can_coexist_across_providers_and_legacy_selectors_disambiguate(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(helper.SCHEMA)
        email = "same@example.test"
        conn.execute("INSERT INTO accounts(id,user,identity,provider,server) VALUES(?,?,?,?,?)",
                     (email, email, email, "icloud", helper.ICLOUD))
        google_id = "google:verified-sub"
        conn.execute("INSERT INTO accounts(id,user,identity,provider,server) VALUES(?,?,?,?,?)",
                     (google_id, email, "verified-sub", "google", helper.GOOGLE_CALDAV_BASE))
        conn.commit()
        self.assertEqual(helper.resolve_account_id(conn, email, "icloud"), email)
        self.assertEqual(helper.resolve_account_id(conn, email, "google"), google_id)
        with self.assertRaises(helper.Failure) as ambiguous:
            helper.resolve_account_id(conn, email)
        self.assertEqual(ambiguous.exception.code, "ambiguous")
        self.assertNotEqual(
            helper.account_identity("caldav", email, "https://dav-one.example/caldav/"),
            helper.account_identity("caldav", email, "https://dav-two.example/caldav/"),
            "same user names on separate CalDAV servers are distinct identities",
        )
        conn.close()


    def test_google_login_uses_verified_sub_as_stable_id_and_reports_calendar_collision(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(helper.SCHEMA)
        conn.execute("INSERT INTO accounts(id,user,identity,provider,server) "
                     "VALUES('legacy-owner','owner@example.test','owner@example.test','icloud',?)",
                     (helper.ICLOUD,))
        shared_url = helper.GOOGLE_CALDAV_BASE + "shared%40example.test/events/"
        conn.execute("INSERT INTO calendars(url,name,account) VALUES(?,?,?)",
                     (shared_url, "Shared", "legacy-owner"))
        conn.commit()
        auth = types.SimpleNamespace(refresh_token="refresh-secret")
        tokens = {}
        hints = []
        mutation_lock = {"held": False}
        identity = {"sub": "stable-sub-44", "email": "new@example.test"}
        found = {"home": helper.GOOGLE_CALDAV_BASE, "calendars": [
            {"url": shared_url, "href": "shared", "name": "Shared", "color": "",
             "readonly": True},
            {"url": helper.GOOGLE_CALDAV_BASE + "new%40example.test/events/",
             "href": "new", "name": "New", "color": "", "readonly": False},
        ]}

        def authorize(hint, _client):
            self.assertFalse(mutation_lock["held"],
                             "the process lock must not span the interactive browser flow")
            hints.append(hint)
            return auth, identity

        @contextmanager
        def tracked_lock(_conn):
            self.assertFalse(mutation_lock["held"])
            mutation_lock["held"] = True
            try:
                yield
            finally:
                mutation_lock["held"] = False

        with patch.object(helper, "google_client_config", return_value={"client_id": "desktop"}), \
             patch.object(helper, "google_authorize", side_effect=authorize), \
             patch.object(helper, "discover_google", return_value=found), \
             patch.object(helper, "stored_google_refresh_token",
                          side_effect=lambda account_id: tokens.get(account_id, "")), \
             patch.object(helper, "store_google_refresh_token",
                          side_effect=lambda account_id, token: tokens.__setitem__(account_id, token)), \
             patch.object(helper, "forget_google_refresh_token",
                          side_effect=lambda account_id: tokens.pop(account_id, None)), \
             patch.object(helper, "state_mutation_lock", tracked_lock):
            first = helper._dispatch_google_login(conn, types.SimpleNamespace(user=""))
            identity["email"] = "renamed@example.test"
            second = helper._dispatch_google_login(conn, types.SimpleNamespace(user=""))

        stable_id = "google:stable-sub-44"
        self.assertEqual(hints, ["", ""], "the UI can start login without asking for an email")
        self.assertEqual(first["accountId"], stable_id)
        self.assertEqual(second["accountId"], stable_id)
        self.assertEqual(second["account"], "renamed@example.test")
        self.assertEqual(first["calendarConflicts"], [{"name": "Shared", "url": shared_url}])
        self.assertTrue(first["warnings"])
        self.assertEqual(conn.execute("SELECT account FROM calendars WHERE url=?", (shared_url,))
                         .fetchone()[0], "legacy-owner")
        self.assertEqual(conn.execute("SELECT user FROM accounts WHERE id=?", (stable_id,))
                         .fetchone()[0], "renamed@example.test")
        self.assertEqual(tokens[stable_id], "refresh-secret")
        self.assertNotIn("refresh-secret", repr(conn.execute("SELECT * FROM accounts").fetchall()))
        conn.close()


class GoogleClientImport(unittest.TestCase):
    def _run_import(self, config_home, selected):
        output = io.StringIO()
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": config_home}, clear=False), \
             patch.object(helper, "pick_file", return_value=selected), \
             patch.object(helper, "db", return_value=sqlite3.connect(":memory:")), \
             redirect_stdout(output):
            status = helper.main(["import-google-client"])
        self.assertEqual(status, 0)
        return json.loads(output.getvalue())

    def test_cli_import_copies_valid_client_with_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            config_home = os.path.join(temporary, "config")
            source = os.path.join(temporary, "client.json")
            original_override = os.path.join(temporary, "explicit-client.json")
            payload = json.dumps({"installed": {
                "client_id": "desktop.apps.googleusercontent.com",
                "client_secret": "client-secret",
            }}).encode()
            with open(source, "wb") as stream:
                stream.write(payload)
            with open(original_override, "wb") as stream:
                stream.write(b"leave explicit override alone")
            selected = {"ok": True, "picked": True, "path": source}

            with patch.dict(os.environ, {"OMARCAL_GOOGLE_CLIENT_FILE": original_override},
                            clear=False):
                result = self._run_import(config_home, selected)

            destination = os.path.join(config_home, "omarcal", "google-client.json")
            self.assertEqual(result, {"ok": True, "imported": True, "path": destination})
            with open(destination, "rb") as stream:
                self.assertEqual(stream.read(), payload)
            self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
            with open(original_override, "rb") as stream:
                self.assertEqual(stream.read(), b"leave explicit override alone")

    def test_cli_cancel_returns_imported_false_without_creating_a_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self._run_import(temporary, {"ok": True, "picked": False})
            self.assertEqual(result, {"ok": True, "imported": False, "cancelled": True})
            self.assertFalse(os.path.exists(os.path.join(
                temporary, "omarcal", "google-client.json")))

    def test_cli_import_rejects_invalid_json_without_replacing_existing_client(self):
        invalid_payloads = [
            b"not JSON",
            json.dumps({"web": {"client_id": "desktop.apps.googleusercontent.com",
                                "client_secret": "bad"}}).encode(),
            json.dumps({"installed": {"client_id": "not-a-desktop-client",
                                       "client_secret": "bad"}}).encode(),
            json.dumps({"installed": {"client_id": "desktop.apps.googleusercontent.com",
                                       "client_secret": 7}}).encode(),
            b"{" + b" " * (helper.GOOGLE_CLIENT_MAX_BYTES + 1),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            destination = os.path.join(temporary, "omarcal", "google-client.json")
            os.makedirs(os.path.dirname(destination))
            with open(destination, "wb") as stream:
                stream.write(b"previous valid client")
            for index, payload in enumerate(invalid_payloads):
                with self.subTest(index=index):
                    source = os.path.join(temporary, f"invalid-{index}.json")
                    with open(source, "wb") as stream:
                        stream.write(payload)
                    result = self._run_import(
                        temporary, {"ok": True, "picked": True, "path": source})
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["code"], "google-config")
                    with open(destination, "rb") as stream:
                        self.assertEqual(stream.read(), b"previous valid client")


class GoogleCalendarAndTransport(unittest.TestCase):
    def test_calendar_list_paginates_and_maps_roles_to_collision_safe_urls(self):
        class CalendarList:
            def __init__(self):
                self.paths = []

            def api_get(self, path):
                self.paths.append(path)
                if len(self.paths) == 1:
                    payload = {"items": [{"id": "writer@example.test", "summary": "Work",
                                          "accessRole": "writer", "primary": True}],
                               "nextPageToken": "page 2"}
                else:
                    payload = {"items": [{"id": "readonly/shared", "summary": "Shared",
                                          "accessRole": "reader"}]}
                return 200, json.dumps(payload).encode()

        auth = CalendarList()
        found = helper.discover_google(auth)
        self.assertEqual(len(auth.paths), 2)
        self.assertIn("pageToken=page+2", auth.paths[1])
        self.assertEqual(found["calendars"][0]["url"],
                         helper.GOOGLE_CALDAV_BASE + "writer%40example.test/events/")
        self.assertFalse(found["calendars"][0]["readonly"])
        self.assertEqual(found["calendars"][1]["url"],
                         helper.GOOGLE_CALDAV_BASE + "readonly%2Fshared/events/")
        self.assertTrue(found["calendars"][1]["readonly"])
        self.assertEqual(found["principal"],
                         helper.GOOGLE_CALDAV_BASE + "writer%40example.test/user")

    def test_authenticated_write_refreshes_after_401_and_refuses_untrusted_urls(self):
        class RotatingAuth:
            def __init__(self):
                self.token = "stale-token"

            def access_token(self):
                return self.token

            def refresh_after_unauthorized(self, rejected):
                self.asserted = rejected
                self.token = "fresh-token"
                return self.token

        class Response:
            def __init__(self, status):
                self.status = status

            def getheaders(self):
                return []

            def read(self, _size):
                return b""

        class Connection:
            def __init__(self):
                self.sent = []

            def request(self, method, path, body=None, headers=None):
                self.sent.append((method, path, body, dict(headers or {})))

            def getresponse(self):
                return Response(401 if len(self.sent) == 1 else 201)

        auth = RotatingAuth()
        session = helper.DavSession("", "", helper.GOOGLE_CALDAV_BASE,
                                    provider="google", google_auth=auth)
        connection = Connection()
        session._connection = lambda _host: connection
        url = helper.GOOGLE_CALDAV_BASE + "user%40example.test/events/event.ics"
        status, _headers, _body = helper.send(
            session, "PUT", url, b"event",
            {"Content-Type": "text/calendar", "If-Match": '"v1"'},
        )
        self.assertEqual(status, 201)
        self.assertEqual([item[3]["Authorization"] for item in connection.sent],
                         ["Bearer stale-token", "Bearer fresh-token"])
        self.assertEqual(auth.asserted, "stale-token")
        self.assertTrue(all(item[3]["If-Match"] == '"v1"' for item in connection.sent))
        with self.assertRaises(helper.Failure) as untrusted:
            helper.send(session, "PUT", "https://attacker.invalid/cal.ics", b"event",
                        {"If-Match": '"v1"'})
        self.assertEqual(untrusted.exception.code, "dav-origin")
        self.assertEqual(len(connection.sent), 2,
                         "origin rejection must happen before sending any Bearer request")
        with self.assertRaises(helper.Failure):
            helper.send(session, "PUT", "https://apidata.googleusercontent.com/not-caldav/x",
                        b"event", {"If-Match": '"v1"'})
        with self.assertRaises(helper.Failure):
            helper.send(session, "PUT", url, b"event",
                        {"Authorization": "Basic secret", "If-Match": '"v1"'})

    def test_google_create_is_refused_until_conditional_create_is_supported(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(helper.SCHEMA)
        account_id = "google:stable-sub"
        calendar = helper.GOOGLE_CALDAV_BASE + "user%40example.test/events/"
        conn.execute("INSERT INTO accounts(id,user,identity,provider,server) VALUES(?,?,?,?,?)",
                     (account_id, "user@example.test", "stable-sub", "google",
                      helper.GOOGLE_CALDAV_BASE))
        conn.execute("INSERT INTO calendars(url,account) VALUES(?,?)", (calendar, account_id))
        conn.commit()
        request = {"calendarUrl": calendar, "targetCalendarUrl": calendar}
        op = {"method": "PUT", "url": calendar + "/new.ics", "body": "event",
              "ifNoneMatch": True}
        with patch.object(helper, "base_of", return_value=""), \
             patch.object(helper, "plan_save", return_value=[op]):
            with self.assertRaises(helper.Failure) as caught:
                helper.send_event(conn, request, False)
        self.assertEqual(caught.exception.code, "unsupported")
        self.assertIn("conditional creation", caught.exception.message)
        conn.close()


if __name__ == "__main__":
    unittest.main()
