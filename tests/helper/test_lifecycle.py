"""Account teardown and competing helper processes, with a private cache."""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import threading
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys_path = os.path.dirname(os.path.abspath(__file__))
HELPER = os.path.join(sys_path, "..", "..", "helper", "orchard-helper")
_loader = importlib.machinery.SourceFileLoader("orchard_helper_lifecycle", HELPER)
_spec = importlib.util.spec_from_loader("orchard_helper_lifecycle", _loader)
helper = importlib.util.module_from_spec(_spec)
_loader.exec_module(helper)

ACCOUNT = "me@example.com"
ACCOUNT_CAL = "https://example.invalid/home/account/"
OTHER_CAL = "https://example.invalid/home/other/"
ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.folder.name, "cache.db")
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(helper.SCHEMA)
        self.conn.execute("ALTER TABLE objects ADD COLUMN pending INTEGER NOT NULL DEFAULT 0")

    def tearDown(self):
        self.conn.close()
        self.folder.cleanup()

    def add_accounts(self):
        self.conn.execute(
            "INSERT INTO accounts(id,user,identity,provider,server) VALUES(?,?,?,?,?)",
            (ACCOUNT, ACCOUNT, ACCOUNT.casefold(), "icloud", helper.ICLOUD),
        )
        self.conn.execute(
            "INSERT INTO calendars(url,name,account) VALUES(?,?,?)",
            (ACCOUNT_CAL, "Account", ACCOUNT),
        )
        self.conn.execute(
            "INSERT INTO calendars(url,name,account) VALUES(?,?,?)",
            (OTHER_CAL, "Other", "other@example.com"),
        )
        self.conn.commit()

    def remove_account(self):
        return helper.dispatch(
            self.conn, argparse.Namespace(command="remove-account", user=ACCOUNT)
        )

    def test_disconnect_purges_queued_account_data_without_restoring_it(self):
        self.add_accounts()
        account_href = ACCOUNT_CAL + "event.ics"
        other_href = OTHER_CAL + "optimistic.ics"
        self.conn.execute(
            "INSERT INTO objects(url,calendar,ics,uid,pending) VALUES(?,?,?,?,1)",
            (account_href, ACCOUNT_CAL, ICS, "account-event"),
        )
        self.conn.execute(
            "INSERT INTO objects(url,calendar,ics,uid,pending) VALUES(?,?,?,?,1)",
            (other_href, OTHER_CAL, ICS, "other-optimistic"),
        )
        pending_id = self.conn.execute(
            "INSERT INTO pending(action,request,title,created) VALUES(?,?,?,0)",
            ("save", json.dumps({"href": account_href, "calendarUrl": ACCOUNT_CAL,
                                 "targetCalendarUrl": OTHER_CAL}), "Moved event"),
        ).lastrowid
        self.conn.executemany(
            "INSERT INTO pending_backup(pending_id,url,existed,calendar,etag,ics) "
            "VALUES(?,?,?,?,?,?)",
            [(pending_id, account_href, 1, ACCOUNT_CAL, '"old"', ICS),
             (pending_id, other_href, 0, "", "", "")],
        )
        self.conn.commit()

        saved_forget, saved_reclaim = helper.forget_password, helper.reclaim
        helper.forget_password = lambda _account: None
        helper.reclaim = lambda _conn: None
        try:
            self.remove_account()
            helper.undo_pending(self.conn, pending_id)
            flushed = helper.flush_pending(self.conn)
        finally:
            helper.forget_password, helper.reclaim = saved_forget, saved_reclaim

        self.assertEqual(self.conn.execute("SELECT url FROM objects").fetchall(), [])
        self.assertEqual(self.conn.execute("SELECT id FROM pending").fetchall(), [])
        self.assertEqual(self.conn.execute("SELECT pending_id FROM pending_backup").fetchall(), [])
        self.assertEqual(flushed, {"sent": 0})

    def test_keyring_clear_failure_keeps_account_data_and_fails_disconnect(self):
        with patch.object(helper.subprocess, "run", return_value=SimpleNamespace(returncode=1)):
            with self.assertRaises(helper.Failure) as clear_error:
                helper.forget_password(ACCOUNT)
        self.assertEqual(clear_error.exception.code, "keyring")

        self.add_accounts()
        self.conn.execute(
            "INSERT INTO objects(url,calendar,ics,uid) VALUES(?,?,?,?)",
            (ACCOUNT_CAL + "event.ics", ACCOUNT_CAL, ICS, "account-event"),
        )
        self.conn.commit()
        saved_forget = helper.forget_password

        def refused(_account):
            raise helper.Failure("keyring", "could not clear the password")

        helper.forget_password = refused
        try:
            with self.assertRaises(helper.Failure) as caught:
                self.remove_account()
        finally:
            helper.forget_password = saved_forget
        self.assertEqual(caught.exception.code, "keyring")
        self.assertEqual(self.conn.execute("SELECT user FROM accounts").fetchall(), [(ACCOUNT,)])
        self.assertEqual(self.conn.execute("SELECT uid FROM objects").fetchall(),
                         [("account-event",)])

    def test_disconnect_waits_for_an_inflight_sync_before_deleting_its_rows(self):
        self.add_accounts()
        sync_entered = threading.Event()
        release_sync = threading.Event()
        disconnect_reached_keyring = threading.Event()
        failures = []
        saved_sync, saved_forget, saved_reclaim = (
            helper.sync, helper.forget_password, helper.reclaim
        )

        def blocked_sync(conn, _calendar="", _force=False):
            sync_entered.set()
            if not release_sync.wait(3):
                raise AssertionError("test did not release the sync")
            conn.execute(
                "INSERT INTO objects(url,calendar,ics,uid) VALUES(?,?,?,?)",
                (ACCOUNT_CAL + "late.ics", ACCOUNT_CAL, ICS, "late"),
            )
            conn.commit()
            return {"ok": True}

        def note_keyring(_account):
            disconnect_reached_keyring.set()

        helper.sync = blocked_sync
        helper.forget_password = note_keyring
        helper.reclaim = lambda _conn: None

        def invoke(command):
            conn = sqlite3.connect(self.path, timeout=3)
            try:
                args = argparse.Namespace(command=command, calendar="", force=False, user=ACCOUNT)
                helper.dispatch(conn, args)
            except Exception as error:
                failures.append(error)
            finally:
                conn.close()

        sync_thread = threading.Thread(target=invoke, args=("sync",))
        remove_thread = threading.Thread(target=invoke, args=("remove-account",))
        sync_thread.start()
        try:
            self.assertTrue(sync_entered.wait(2), "sync reached its write phase")
            remove_thread.start()
            removed_while_sync_waited = disconnect_reached_keyring.wait(0.25)
        finally:
            release_sync.set()
            sync_thread.join(3)
            if remove_thread.ident is not None:
                remove_thread.join(3)
            helper.sync, helper.forget_password, helper.reclaim = (
                saved_sync, saved_forget, saved_reclaim
            )
        self.assertFalse(removed_while_sync_waited,
                         "disconnect must wait for a running sync's complete write")
        self.assertEqual(failures, [])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM accounts").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM objects").fetchone()[0], 0)

    def test_two_flushes_do_not_send_the_same_queued_change(self):
        pending_id = self.conn.execute(
            "INSERT INTO pending(action,request,title,created) VALUES('save','{}','Lunch',0)"
        ).lastrowid
        self.conn.execute(
            "INSERT INTO pending_backup(pending_id,url,existed) VALUES(?,?,0)",
            (pending_id, OTHER_CAL + "event.ics"),
        )
        self.conn.commit()
        send_entered = threading.Event()
        second_send_entered = threading.Event()
        release_send = threading.Event()
        count_lock = threading.Lock()
        sent = []
        failures = []
        saved_send = helper.send_event

        def blocked_send(_conn, _request, _deleting):
            with count_lock:
                sent.append(True)
                count = len(sent)
            if count == 1:
                send_entered.set()
            elif count == 2:
                second_send_entered.set()
            if not release_send.wait(3):
                raise AssertionError("test did not release the queued write")
            return {"ok": True}

        helper.send_event = blocked_send

        def flush():
            conn = sqlite3.connect(self.path, timeout=3)
            try:
                helper.dispatch(conn, argparse.Namespace(command="flush"))
            except Exception as error:
                failures.append(error)
            finally:
                conn.close()

        first = threading.Thread(target=flush)
        second = threading.Thread(target=flush)
        first.start()
        try:
            self.assertTrue(send_entered.wait(2), "first flush began sending")
            second.start()
            second_sent_while_first_waited = second_send_entered.wait(0.25)
        finally:
            release_send.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
            helper.send_event = saved_send
        self.assertFalse(second_sent_while_first_waited,
                         "only one helper process may own a queued write")
        self.assertEqual(failures, [])
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM pending").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
