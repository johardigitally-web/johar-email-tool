"""What this tool must refuse to do.

Almost nothing here checks that a feature works. It checks that the tool will not
mail somebody who never opted in, will not mail the same person twice, will not
send with the switch off, will not send when the unsubscribe link would be
broken, and cannot be turned into an open redirect. There is no undo on a sent
email, so the refusals are the part worth testing.

Run:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import builtins
import datetime
import html as html_mod
import json
import os
import re
import shutil
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlencode
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mailer import config  # noqa: E402

# Point everything at a scratch database and a plausible public host before the
# app is imported, so no test can touch a real file or a real mail server.
_tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
_tmp.close()
config.DB_PATH = _tmp.name
# A password while the app is imported, because the startup guard refuses to
# come up on a public address with the login switched off, and it is right to.
# Cleared again below so the page tests can stay simple: what they exercise is
# the screens, not the door.
config.PASSWORD = "for-the-startup-guard"
config.PUBLIC_URL = "https://mail.example.com"
config.SECRET_KEY = "test-secret-not-the-source-default"
config.POSTAL_ADDRESS = "Your Shop BV, Example City"
config.SEND_PAUSE = 0

from mailer import copy as mailcopy  # noqa: E402
from mailer import (attrib, bounces, carts, crm, db, discounts, flows,  # noqa: E402
                    layouts,
                    send as sender,
                    shop, sources)
import app as webapp  # noqa: E402

config.PASSWORD = ""          # see above: the guard has done its job by now


class Base(unittest.TestCase):
    def setUp(self):
        for t in ("send", "campaign", "subscriber", "segment"):
            try:
                self.conn0 = db.connect()
                self.conn0.execute("DROP TABLE IF EXISTS " + t)
                self.conn0.commit()
                self.conn0.close()
            except Exception:
                pass
        db.init()
        self.conn = db.connect()
        config.SENDING_ENABLED = True
        config.SMTP_HOST = "smtp.example.com"
        config.DAILY_CAP = 300
        config.BATCH_SIZE = 25
        # The real .env locks sending to one address. Left alone, a test only
        # passed because an earlier CLASS had happened to clear it, which is an
        # order dependency that hid a genuine failure for a while.
        config.ALLOWED_RECIPIENTS = []
        self.sent = []

    def tearDown(self):
        self.conn.close()

    def sub(self, email, consent=db.YES, **kw):
        db.upsert_subscriber(self.conn, email, consent=consent, **kw)
        self.conn.commit()
        return self.conn.execute("SELECT * FROM subscriber WHERE email = ?",
                                 (email.lower(),)).fetchone()

    def campaign(self, body='<p>Hallo {{naam}}</p><a href="https://example.com/x">Kijk</a>'):
        cur = self.conn.execute(
            "INSERT INTO campaign (name, subject, body, audience, created)"
            " VALUES ('C','Onderwerp',?,'all',?)", (body, db.now()))
        self.conn.commit()
        return self.conn.execute("SELECT * FROM campaign WHERE id = ?",
                                 (cur.lastrowid,)).fetchone()

    def fake_smtp(self):
        outer = self

        class FakeServer:
            def send_message(self, msg):
                outer.sent.append(msg)

            def quit(self):
                pass

        return mock.patch.object(sender, "_smtp", return_value=FakeServer())


class ConsentTests(Base):
    def test_an_address_is_not_consent(self):
        """The default for a known address is 'never', not 'subscribed'."""
        s = self.sub("weet@x.nl", consent=db.NEVER)
        self.assertEqual(s["consent"], db.NEVER)
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)

    def test_no_audience_reaches_someone_who_opted_out(self):
        self.sub("ja@x.nl")
        out = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, out["id"])
        self.conn.commit()
        for name, _label in sender.AUDIENCES:
            where, params = sender.audience_sql(name)
            rows = self.conn.execute(
                "SELECT email FROM subscriber WHERE " + where, params).fetchall()
            self.assertNotIn("weg@x.nl", [r["email"] for r in rows], name)

    def test_a_bounced_address_is_never_mailed_again(self):
        s = self.sub("dood@x.nl")
        db.mark_bounced(self.conn, s["id"], "550 unknown")
        self.conn.commit()
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)

    def test_an_import_can_never_resurrect_an_unsubscribe(self):
        s = self.sub("terug@x.nl")
        db.unsubscribe(self.conn, s["id"])
        self.conn.commit()
        outcome, _ = db.upsert_subscriber(self.conn, "terug@x.nl", consent=db.YES,
                                          source="shopify")
        self.conn.commit()
        self.assertEqual(outcome, "kept_unsubscribed")
        again = self.conn.execute("SELECT consent FROM subscriber WHERE email='terug@x.nl'").fetchone()
        self.assertEqual(again["consent"], db.NO)


class QueueTests(Base):
    def test_queue_is_idempotent(self):
        camp = self.campaign()
        self.sub("a@x.nl")
        self.sub("b@x.nl")
        self.assertEqual(sender.queue(self.conn, camp["id"], "all"), 2)
        self.assertEqual(sender.queue(self.conn, camp["id"], "all"), 0)
        self.sub("c@x.nl")
        self.assertEqual(sender.queue(self.conn, camp["id"], "all"), 1)

    def test_the_database_refuses_a_second_copy(self):
        import sqlite3
        camp = self.campaign()
        s = self.sub("a@x.nl")
        self.conn.execute("INSERT INTO send (campaign_id, subscriber_id, to_email, token)"
                          " VALUES (?,?,?,?)", (camp["id"], s["id"], s["email"], db.token()))
        self.conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO send (campaign_id, subscriber_id, to_email, token)"
                              " VALUES (?,?,?,?)", (camp["id"], s["id"], s["email"], db.token()))


class GuardTests(Base):
    def test_each_guard_reports_itself(self):
        camp = self.campaign()
        config.SENDING_ENABLED = False
        self.assertEqual(sender.blocked_reason(self.conn, camp), "disabled")
        config.SENDING_ENABLED = True
        config.SMTP_HOST = ""
        self.assertEqual(sender.blocked_reason(self.conn, camp), "no_smtp")
        config.SMTP_HOST = "smtp.example.com"
        old = config.POSTAL_ADDRESS
        config.POSTAL_ADDRESS = "   "
        self.assertEqual(sender.blocked_reason(self.conn, camp), "no_postal_address")
        config.POSTAL_ADDRESS = old
        old_url = config.PUBLIC_URL
        config.PUBLIC_URL = "http://127.0.0.1:5000"
        self.assertEqual(sender.blocked_reason(self.conn, camp), "public_url_is_local")
        config.PUBLIC_URL = old_url
        self.assertIsNone(sender.blocked_reason(self.conn, camp))

    def test_a_local_public_url_blocks_sending(self):
        """A newsletter whose unsubscribe link points at somebody's laptop is
        illegal as well as useless."""
        config.PUBLIC_URL = "http://localhost:5000"
        self.assertFalse(config.public_url_is_reachable())
        config.PUBLIC_URL = "https://mail.example.com"
        self.assertTrue(config.public_url_is_reachable())


class RenderTests(Base):
    def test_the_message_carries_what_keeps_it_out_of_spam(self):
        camp = self.campaign()
        html, text = sender.render(camp, "Sam", "tok123")
        self.assertIn("Hallo Sam", html)
        self.assertIn("Your Shop BV", html)
        self.assertIn("Your Shop BV", text)
        self.assertIn("/u/tok123", html)
        self.assertIn("/u/tok123", text)
        self.assertIn("/p/tok123.gif", html)
        self.assertTrue(text.strip())

    def test_links_are_tracked_but_the_unsubscribe_link_is_not(self):
        camp = self.campaign()
        html, _ = sender.render(camp, "", "tok123")
        self.assertIn("/c/tok123?u=https", html)
        self.assertIn('href="%s/u/tok123"' % config.PUBLIC_URL, html)

    def test_headers_include_one_click_unsubscribe(self):
        camp = self.campaign()
        html, text = sender.render(camp, "A", "tok123")
        msg = sender.build_message(camp, "a@x.nl", html, text, "tok123")
        self.assertIn("/u/tok123", msg["List-Unsubscribe"])
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")
        self.assertTrue(msg.is_multipart())


class SendTests(Base):
    def test_someone_who_leaves_after_queueing_is_not_mailed(self):
        camp = self.campaign()
        s = self.sub("weg@x.nl")
        sender.queue(self.conn, camp["id"], "all")
        db.unsubscribe(self.conn, s["id"])
        self.conn.commit()
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?", (db.SENDING, camp["id"]))
        self.conn.commit()
        with self.fake_smtp():
            result = sender.send_batch(self.conn, camp["id"])
        self.assertEqual(result["sent"], 0)
        self.assertEqual(len(self.sent), 0)

    def test_sending_resumes_rather_than_restarts(self):
        camp = self.campaign()
        for i in range(5):
            self.sub("p%s@x.nl" % i)
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?", (db.SENDING, camp["id"]))
        self.conn.commit()
        config.BATCH_SIZE = 2
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"])
            self.assertEqual(len(self.sent), 2)
            sender.send_batch(self.conn, camp["id"])
            sender.send_batch(self.conn, camp["id"])
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(len({m["To"] for m in self.sent}), 5)

    def test_daily_cap_stops_a_runaway(self):
        camp = self.campaign()
        for i in range(6):
            self.sub("q%s@x.nl" % i)
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?", (db.SENDING, camp["id"]))
        self.conn.commit()
        config.DAILY_CAP = 3
        with self.fake_smtp():
            result = sender.send_batch(self.conn, camp["id"])
        self.assertEqual(result["stopped"], "daily_cap")
        self.assertLessEqual(len(self.sent), 3)

    def test_a_finished_campaign_is_marked_sent(self):
        camp = self.campaign()
        self.sub("een@x.nl")
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?", (db.SENDING, camp["id"]))
        self.conn.commit()
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"])
        row = self.conn.execute("SELECT status FROM campaign WHERE id = ?", (camp["id"],)).fetchone()
        self.assertEqual(row["status"], db.SENT)


class QueueProgressTests(Base):
    """A queue that cannot finish, and a race that can send twice. Both were
    found by an outside review of the code, and both are the kind of fault that
    only shows up on the day it matters."""

    def queue_for(self, emails):
        camp = self.campaign()
        for e in emails:
            self.sub(e)
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                          (db.SENDING, camp["id"]))
        self.conn.commit()
        return camp

    def test_a_campaign_can_be_sent_again_tomorrow(self):
        """The warm-up is twelve lists over twelve days. A campaign used to be
        marked finished the instant its first batch drained and then refused to
        send ever again, so the ramp had no day two."""
        camp = self.queue_for(["een@x.nl", "twee@x.nl", "drie@x.nl"])
        with self.fake_smtp():
            first = sender.send_batch(self.conn, camp["id"], limit=1)
        self.assertEqual(first["sent"], 1)
        status = self.conn.execute("SELECT status FROM campaign WHERE id = ?",
                                   (camp["id"],)).fetchone()["status"]
        self.assertNotEqual(status, db.SENT, "closed itself with 2 people left")
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"], limit=5)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM campaign WHERE id = ?",
            (camp["id"],)).fetchone()["status"], db.SENT)

    def test_a_campaign_finishes_even_when_somebody_cannot_be_mailed(self):
        """They unsubscribed after being queued. The row can never be sent, so
        counting it as outstanding leaves the campaign stuck for ever."""
        camp = self.queue_for(["een@x.nl", "twee@x.nl"])
        self.conn.execute("UPDATE subscriber SET consent = ? WHERE email = ?",
                          (db.NO, "een@x.nl"))
        self.conn.commit()
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sender.pending_count(self.conn, camp["id"]), 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM campaign WHERE id = ?",
            (camp["id"],)).fetchone()["status"], db.SENT)

    def test_an_unsendable_row_does_not_block_everyone_behind_it(self):
        """The batch takes the lowest ids first. Twenty five dead addresses at
        the front used to mean nobody after them ever got the email."""
        camp = self.queue_for(["dood@x.nl", "levend@x.nl"])
        self.conn.execute("UPDATE subscriber SET bounced = 1 WHERE email = ?",
                          ("dood@x.nl",))
        self.conn.commit()
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"], limit=1)   # only the dead one
            sender.send_batch(self.conn, camp["id"], limit=1)   # must move on
        self.assertEqual([m["To"] for m in self.sent], ["levend@x.nl"])

    def test_a_terminal_skip_says_why(self):
        camp = self.queue_for(["weg@x.nl"])
        self.conn.execute("UPDATE subscriber SET consent = ? WHERE email = ?",
                          (db.NO, "weg@x.nl"))
        self.conn.commit()
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"])
        row = self.conn.execute("SELECT skipped, error FROM send").fetchone()
        self.assertEqual(row["skipped"], "not_mailable")

    # --- the race -----------------------------------------------------------

    def test_two_runs_at_once_never_send_to_the_same_person_twice(self):
        """Both used to SELECT the same unsent rows and both hand them over.
        One double click on the send button was enough."""
        camp = self.queue_for(["een@x.nl", "twee@x.nl", "drie@x.nl"])
        other = db.connect()
        try:
            with self.fake_smtp():
                # Interleaved on purpose: the first claims, the second must
                # find nothing left to take.
                first = sender.send_batch(self.conn, camp["id"], limit=3)
                second = sender.send_batch(other, camp["id"], limit=3)
        finally:
            other.close()
        self.assertEqual(first["sent"], 3)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len({m["To"] for m in self.sent}), 3)

    def test_a_claim_is_taken_before_anything_is_handed_to_the_mail_server(self):
        """The ordering is the whole fix: claim, then send. Proved by looking
        at the row from a SECOND connection while the send is in progress."""
        camp = self.queue_for(["een@x.nl"])
        other = db.connect()
        seen = {}

        class Watcher:
            def send_message(self, msg, from_addr=None):
                seen["claimed"] = other.execute(
                    "SELECT claim FROM send").fetchone()["claim"]

            def quit(self):
                pass

        try:
            with mock.patch.object(sender, "_smtp", return_value=Watcher()):
                sender.send_batch(self.conn, camp["id"])
        finally:
            other.close()
        self.assertTrue(seen["claimed"], "row was not claimed before sending")

    def test_a_run_that_died_does_not_strand_its_batch_for_ever(self):
        camp = self.queue_for(["een@x.nl"])
        # A claim from a run that never came back.
        self.conn.execute(
            "UPDATE send SET claim = 'dead-run', claimed_at = ?",
            ((datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=2)).isoformat(timespec="seconds"),))
        self.conn.commit()
        with self.fake_smtp():
            out = sender.send_batch(self.conn, camp["id"])
        self.assertEqual(out["sent"], 1)

    def test_a_fresh_claim_is_left_alone(self):
        camp = self.queue_for(["een@x.nl"])
        self.conn.execute("UPDATE send SET claim = 'other-run', claimed_at = ?",
                          (db.now(),))
        self.conn.commit()
        with self.fake_smtp():
            out = sender.send_batch(self.conn, camp["id"])
        self.assertEqual(out["sent"], 0)
        self.assertEqual(self.sent, [])

    def test_what_a_run_could_not_send_is_handed_back(self):
        """Otherwise the next press waits fifteen minutes for the lease."""
        camp = self.queue_for(["een@x.nl"])
        with mock.patch.object(sender, "_smtp", side_effect=OSError("down")):
            sender.send_batch(self.conn, camp["id"])
        self.assertEqual(self.conn.execute(
            "SELECT claim FROM send").fetchone()["claim"], "")
        with self.fake_smtp():
            out = sender.send_batch(self.conn, camp["id"])
        self.assertEqual(out["sent"], 1)

    def test_the_allowlist_stops_the_run_instead_of_spinning(self):
        camp = self.queue_for(["een@x.nl"])
        config.ALLOWED_RECIPIENTS = ["iemand.anders@x.nl"]
        try:
            with self.fake_smtp():
                out = sender.send_batch(self.conn, camp["id"])
        finally:
            config.ALLOWED_RECIPIENTS = []
        self.assertEqual(out["stopped"], "not_on_allowlist")
        self.assertEqual(self.sent, [])


class SchemaUpgradeTests(Base):
    """Several gunicorn workers boot at once and every one of them runs the
    schema upgrade. Two can both find a column missing and both add it; the
    loser used to crash, and a crashed worker takes the service down. This took
    the live site offline once."""

    def test_two_workers_upgrading_at_once_do_not_kill_each_other(self):
        import sqlite3 as _sqlite
        a, b = db.connect(), db.connect()
        try:
            a.execute("ALTER TABLE subscriber ADD COLUMN race_test TEXT")
            a.commit()
            # The second worker looked BEFORE the first had finished, so it
            # still believes the column is missing. It must survive being wrong.
            try:
                b.execute("ALTER TABLE subscriber ADD COLUMN race_test TEXT")
                self.fail("expected sqlite to refuse the duplicate")
            except _sqlite.OperationalError as exc:
                self.assertIn("duplicate column", str(exc).lower())
            db.init()          # the real path, which must not raise
        finally:
            a.close()
            b.close()


class FooterTruthTests(Base):
    """What the footer claims about WHY somebody is receiving this. It used to
    tell all 5,724 people they had signed up for the newsletter, which is untrue
    for the 2,233 who never did, and an email that states a false basis is the
    complainant's evidence rather than the shop's defence."""

    def reason(self, **kw):
        self.sub("klant@x.nl")
        sets, vals = [], []
        for k, v in kw.items():
            sets.append("%s = ?" % k)
            vals.append(v)
        if sets:
            self.conn.execute("UPDATE subscriber SET %s WHERE email = ?" % ", ".join(sets),
                              vals + ["klant@x.nl"])
            self.conn.commit()
        row = self.conn.execute("SELECT * FROM subscriber WHERE email = ?",
                                ("klant@x.nl",)).fetchone()
        return sender.reason_for(row)

    def test_somebody_who_opted_in_is_told_they_opted_in(self):
        self.assertEqual(self.reason(source="shopify"), sender.REASON_NEWSLETTER)

    def test_a_buyer_is_told_they_bought_not_that_they_subscribed(self):
        r = self.reason(source="customer", spent=1995)
        self.assertEqual(r, sender.REASON_CUSTOMER)
        self.assertIn("eerder bij ons heeft gekocht", r)

    def test_a_hand_marked_address_is_never_told_it_signed_up(self):
        """The 2,233. Whatever else happens to them, the email must not claim a
        signup that never happened."""
        self.assertNotEqual(self.reason(source="manual", spent=0),
                            sender.REASON_NEWSLETTER)

    def test_an_unknown_person_gets_the_weaker_claim(self):
        self.assertEqual(sender.reason_for(None), sender.REASON_CUSTOMER)

    def test_a_real_campaign_carries_the_right_reason_per_person(self):
        camp = self.campaign()
        opted = self.sub("optin@x.nl")
        self.conn.execute("UPDATE subscriber SET source = 'shopify' WHERE id = ?",
                          (opted["id"],))
        self.conn.commit()
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                          (db.SENDING, camp["id"]))
        self.conn.commit()
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"])
        html = self.sent[0].get_body(("html",)).get_content()
        self.assertIn("aangemeld voor onze nieuwsbrief", html)

    def test_the_text_part_still_carries_the_way_out(self):
        camp = self.campaign()
        _html, text = sender.render(camp, "Sam", "TOK")
        self.assertIn("Afmelden:", text)


class StartupGuardTests(Base):
    """It must not come up unprotected on an address anybody can reach. Found
    by an outside review: an empty password did not mean "no login needed", it
    meant the login was switched off, and the fallback session secret is
    printed in the source for anyone to read."""

    def setUp(self):
        super().setUp()
        self.saved = (config.PUBLIC_URL, config.PASSWORD, config.SECRET_KEY)

    def tearDown(self):
        config.PUBLIC_URL, config.PASSWORD, config.SECRET_KEY = self.saved
        super().tearDown()

    def test_a_public_address_with_no_password_refuses(self):
        config.PUBLIC_URL = "https://mail.example.com"
        config.PASSWORD = ""
        config.SECRET_KEY = "a-real-secret"
        self.assertIn("MAILER_PASSWORD", config.refuse_to_start())

    def test_a_public_address_with_the_source_secret_refuses(self):
        config.PUBLIC_URL = "https://mail.example.com"
        config.PASSWORD = "a-real-password"
        config.SECRET_KEY = config.DEV_SECRET
        self.assertIn("MAILER_SECRET", config.refuse_to_start())

    def test_a_public_address_set_up_properly_starts(self):
        config.PUBLIC_URL = "https://mail.example.com"
        config.PASSWORD = "a-real-password"
        config.SECRET_KEY = "a-real-secret"
        self.assertEqual(config.refuse_to_start(), "")

    def test_a_laptop_is_left_alone(self):
        """No password on 127.0.0.1 is a convenience, not a hole."""
        config.PUBLIC_URL = "http://127.0.0.1:5000"
        config.PASSWORD = ""
        config.SECRET_KEY = config.DEV_SECRET
        self.assertEqual(config.refuse_to_start(), "")


class CartCeilingTests(Base):
    """What a forger can cost us. The endpoint is public and nothing a browser
    says can be proved, so the question is not whether it can be forged but how
    far that gets somebody."""

    def setUp(self):
        super().setUp()
        for t in ("cart_event",):
            try:
                self.conn.execute("DELETE FROM " + t)
            except Exception:
                pass
        self.conn.commit()

    def test_a_forger_cannot_enrol_the_whole_list(self):
        """The per-person limit does nothing against somebody working through a
        list of addresses, so there is a ceiling across everybody."""
        old = carts.HOURLY_CEILING
        carts.HOURLY_CEILING = 5
        try:
            for i in range(12):
                who = "klant%s@x.nl" % i
                self.sub(who)
                carts.record(self.conn, {"email": who, "items": []})
            self.assertEqual(self.conn.execute(
                "SELECT COUNT(*) FROM cart_event").fetchone()[0], 5)
        finally:
            carts.HOURLY_CEILING = old

    def test_the_refusal_says_why_for_the_log(self):
        old = carts.HOURLY_CEILING
        carts.HOURLY_CEILING = 1
        try:
            for who in ("een@x.nl", "twee@x.nl"):
                self.sub(who)
            carts.record(self.conn, {"email": "een@x.nl", "items": []})
            ok, why = carts.record(self.conn, {"email": "twee@x.nl", "items": []})
            self.assertFalse(ok)
            self.assertIn("ceiling", why)
        finally:
            carts.HOURLY_CEILING = old

    def test_yesterdays_events_do_not_count_against_today(self):
        old = carts.HOURLY_CEILING
        carts.HOURLY_CEILING = 2
        try:
            self.sub("klant@x.nl")
            self.conn.execute(
                "INSERT INTO cart_event (email, source, seen)"
                " VALUES ('oud@x.nl', 'shop', ?), ('ouder@x.nl', 'shop', ?)",
                (db.minutes_ago(300), db.minutes_ago(400)))
            self.conn.commit()
            ok, _why = carts.record(self.conn, {"email": "klant@x.nl", "items": []})
            self.assertTrue(ok)
        finally:
            carts.HOURLY_CEILING = old


class ImportTests(Base):
    def test_csv_without_the_tick_stores_but_does_not_consent(self):
        t = sources.import_csv(self.conn, "a@x.nl\nb@x.nl", assume_consent=False)
        self.assertEqual(t["created"], 2)
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)

    def test_csv_with_the_tick_grants_consent(self):
        sources.import_csv(self.conn, "email,naam\na@x.nl,Sam", assume_consent=True)
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)
        row = self.conn.execute("SELECT name FROM subscriber WHERE email='a@x.nl'").fetchone()
        self.assertEqual(row["name"], "Sam")

    def test_junk_lines_are_skipped_not_stored(self):
        t = sources.import_csv(self.conn, "geen-adres\nook geen\nwel@x.nl", assume_consent=True)
        self.assertEqual(t["skipped"], 2)
        self.assertEqual(t["created"], 1)


class PublicRouteTests(Base):
    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        self.camp = self.campaign()
        s = self.sub("lezer@x.nl")
        self.conn.execute("INSERT INTO send (campaign_id, subscriber_id, to_email, token)"
                          " VALUES (?,?,?,?)", (self.camp["id"], s["id"], s["email"], "TOK"))
        self.conn.commit()

    def test_pixel_records_the_open_and_always_returns_an_image(self):
        r = self.client.get("/p/TOK.gif")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.mimetype, "image/gif")
        row = self.conn.execute("SELECT opened FROM send WHERE token='TOK'").fetchone()
        self.assertIsNotNone(row["opened"])
        # an unknown token must still give an image, not a broken one in an inbox
        self.assertEqual(self.client.get("/p/onbekend.gif").status_code, 200)

    def tracked(self, url, token="TOK"):
        """The link exactly as it appears in an email."""
        return "/c/%s?%s" % (token, urlencode(
            {"u": url, "s": sender.click_signature(token, url)}))

    def test_click_refuses_to_be_an_open_redirect(self):
        bad = self.client.get("/c/TOK?u=javascript:alert(1)")
        self.assertEqual(bad.status_code, 302)
        self.assertNotIn("javascript", bad.headers["Location"])
        good = self.client.get(self.tracked("https://example.com/x"))
        self.assertEqual(good.headers["Location"], "https://example.com/x")
        row = self.conn.execute("SELECT opened, clicked FROM send WHERE token='TOK'").fetchone()
        self.assertIsNotNone(row["clicked"])
        self.assertIsNotNone(row["opened"])

    def test_a_link_we_did_not_write_is_not_followed(self):
        """The whole point. Anybody could put any address after ?u= and our own
        domain would forward to it, which is a phishing link wearing the name
        customers are being taught to trust."""
        r = self.client.get("/c/TOK?u=https://evil.example/login")
        self.assertEqual(r.headers["Location"], config.FALLBACK_URL)
        row = self.conn.execute("SELECT clicked FROM send WHERE token='TOK'").fetchone()
        self.assertIsNone(row["clicked"])      # and it is not counted as a click

    def test_a_signature_from_one_email_does_not_work_in_another(self):
        """Keyed on the token too, so a signature lifted out of one person's
        message cannot be replayed with somebody else's token."""
        url = "https://example.com/x"
        stolen = sender.click_signature("TOK", url)
        r = self.client.get("/c/OTHER?%s" % urlencode({"u": url, "s": stolen}))
        self.assertEqual(r.headers["Location"], config.FALLBACK_URL)

    def test_changing_the_address_invalidates_the_signature(self):
        url = "https://example.com/x"
        sig = sender.click_signature("TOK", url)
        r = self.client.get("/c/TOK?%s" % urlencode(
            {"u": "https://example.com/x-evil", "s": sig}))
        self.assertEqual(r.headers["Location"], config.FALLBACK_URL)

    def test_every_link_in_a_real_email_carries_its_signature(self):
        camp = self.campaign(
            body='<a href="https://example.com/products/netha">Kijk</a>')
        html, _text = sender.render(camp, "Sam", "TOK")
        self.assertIn("s=" + sender.click_signature(
            "TOK", sender._tagged("https://example.com/products/netha",
                                  sender.utm_name(camp), "TOK")), html)

    def test_a_robot_fetching_the_link_does_not_unsubscribe_anybody(self):
        """Defender Safe Links, Proofpoint and Barracuda fetch every URL in an
        inbound message before the human sees it, and 55% of this list is
        behind Microsoft. RFC 8058 says the one-click URI must not act on GET."""
        r = self.client.get("/u/TOK")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Ja, afmelden", r.get_data(as_text=True))
        row = self.conn.execute(
            "SELECT consent FROM subscriber WHERE id = ("
            " SELECT subscriber_id FROM send WHERE token = 'TOK')").fetchone()
        self.assertNotEqual(row["consent"], db.NO)     # untouched

    def test_the_button_on_that_page_does_unsubscribe(self):
        self.client.post("/u/TOK")
        row = self.conn.execute(
            "SELECT consent FROM subscriber WHERE id = ("
            " SELECT subscriber_id FROM send WHERE token = 'TOK')").fetchone()
        self.assertEqual(row["consent"], db.NO)

    def test_unsubscribe_works_on_a_bare_post_and_needs_no_login(self):
        r = self.client.post("/u/TOK")
        self.assertEqual(r.status_code, 200)
        row = self.conn.execute("SELECT consent FROM subscriber WHERE email='lezer@x.nl'").fetchone()
        self.assertEqual(row["consent"], db.NO)

    def test_the_unsubscribe_page_shows_nothing_of_the_tool(self):
        html = self.client.get("/u/TOK").get_data(as_text=True)
        self.assertIn("lezer@x.nl", html)
        self.assertNotIn("Email Marketing", html)
        self.assertNotIn("Campagnes", html)


class StaffPageTests(Base):
    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_the_pages_render(self):
        for url in ("/", "/subscribers", "/campaign/new"):
            self.assertEqual(self.client.get(url).status_code, 200, url)

    def test_a_campaign_can_be_written_and_opened(self):
        r = self.client.post("/campaign/new", data={
            "name": "Herfst", "subject": "Nieuwe collectie",
            "body": "<p>Hallo {{naam}}</p>", "audience": "all"})
        self.assertEqual(r.status_code, 302)
        row = self.conn.execute("SELECT id FROM campaign WHERE name='Herfst'").fetchone()
        self.assertEqual(self.client.get("/campaign/%s" % row["id"]).status_code, 200)


class ApiTests(Base):
    """The endpoints the interface drives itself with."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_preview_renders_without_touching_anything(self):
        r = self.client.post("/api/preview", json={"body": "<p>Hoi {{naam}}</p>",
                                                   "preheader": "kijk"})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn("Hoi Sam", html)
        self.assertIn("Your Shop BV", html)
        # a preview must never look like a real open
        self.assertIn("/p/voorbeeld.gif", html)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM send").fetchone()[0], 0)

    def test_audience_count_is_live_and_rejects_nonsense(self):
        self.sub("a@x.nl")
        self.sub("b@x.nl")
        self.sub("c@x.nl", consent=db.NEVER)
        r = self.client.get("/api/audience/all")
        self.assertEqual(r.get_json()["count"], 2)
        self.assertEqual(self.client.get("/api/audience/verzonnen").status_code, 400)

    def test_send_endpoint_reports_progress_and_finishes(self):
        camp = self.campaign()
        for i in range(5):
            self.sub("s%s@x.nl" % i)
        config.BATCH_SIZE = 2
        with self.fake_smtp():
            first = self.client.post("/api/campaign/%s/send" % camp["id"]).get_json()
            self.assertEqual(first["sent_total"], 2)
            self.assertEqual(first["queued"], 5)
            self.assertFalse(first["done"])
            self.client.post("/api/campaign/%s/send" % camp["id"])
            last = self.client.post("/api/campaign/%s/send" % camp["id"]).get_json()
        self.assertTrue(last["done"])
        self.assertEqual(last["sent_total"], 5)
        self.assertEqual(len(self.sent), 5)

    def test_send_endpoint_refuses_in_plain_language(self):
        camp = self.campaign()
        self.sub("a@x.nl")
        config.SENDING_ENABLED = False
        r = self.client.post("/api/campaign/%s/send" % camp["id"]).get_json()
        self.assertIn("SENDING_ENABLED", r["error"])
        self.assertEqual(len(self.sent), 0)

    def test_send_endpoint_404s_on_a_campaign_that_does_not_exist(self):
        self.assertEqual(self.client.post("/api/campaign/9999/send").status_code, 404)


class TestSendTests(Base):
    """A test send must leave no trace.

    The first version signed you up: it called upsert_subscriber with consent
    YES, so previewing your own newsletter added you to the list, created a
    counted send row, and your own open landed in the campaign's open rate.
    """

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_a_test_send_adds_nobody_and_counts_nothing(self):
        camp = self.campaign()
        before_subs = self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0]
        with self.fake_smtp():
            r = self.client.post("/campaign/%s/test" % camp["id"],
                                 data={"to": "ik@example.com"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["To"], "ik@example.com")
        # nothing written: no subscriber, no send row, no counts
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0],
                         before_subs)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM send").fetchone()[0], 0)
        self.assertEqual(db.campaign_counts(self.conn, camp["id"])["sent"], 0)

    def test_a_test_send_still_carries_the_unsubscribe_header(self):
        camp = self.campaign()
        with self.fake_smtp():
            self.client.post("/campaign/%s/test" % camp["id"], data={"to": "ik@x.nl"})
        msg = self.sent[0]
        self.assertIn("/u/test-", msg["List-Unsubscribe"])
        self.assertEqual(msg["List-Unsubscribe-Post"], "List-Unsubscribe=One-Click")

    def test_a_test_send_works_while_sending_is_switched_off(self):
        """Seeing it in a real inbox is how a broken link gets caught. Requiring
        the live switch would mean the first proper look is also the moment
        everyone else gets it."""
        camp = self.campaign()
        config.SENDING_ENABLED = False
        with self.fake_smtp():
            self.client.post("/campaign/%s/test" % camp["id"], data={"to": "ik@x.nl"})
        self.assertEqual(len(self.sent), 1)

    def test_a_bad_address_sends_nothing(self):
        camp = self.campaign()
        with self.fake_smtp():
            self.client.post("/campaign/%s/test" % camp["id"], data={"to": "geen-adres"})
        self.assertEqual(len(self.sent), 0)


class AllowlistTests(Base):
    """The owner's "do not mail customers" instruction, enforced by code.

    Checked per message inside send_one rather than when a campaign is queued,
    so no route reaches a real customer: not the send loop, not the API, not the
    test button, and not a queue that was built before the list was set.
    """

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        config.ALLOWED_RECIPIENTS = ["owner@example.com"]

    def tearDown(self):
        config.ALLOWED_RECIPIENTS = []
        super().tearDown()

    def test_a_campaign_cannot_reach_anybody_off_the_list(self):
        camp = self.campaign()
        self.sub("klant1@x.nl")
        self.sub("klant2@x.nl")
        self.sub("owner@example.com")
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                          (db.SENDING, camp["id"]))
        self.conn.commit()
        with self.fake_smtp():
            sender.send_batch(self.conn, camp["id"])
        # exactly one message, to the one allowed address
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["To"], "owner@example.com")
        blocked = self.conn.execute(
            "SELECT COUNT(*) FROM send WHERE error = 'not_on_allowlist'").fetchone()[0]
        self.assertEqual(blocked, 2)

    def test_the_test_button_respects_it_too(self):
        camp = self.campaign()
        with self.fake_smtp():
            self.client.post("/campaign/%s/test" % camp["id"], data={"to": "iemand@x.nl"})
        self.assertEqual(len(self.sent), 0)
        with self.fake_smtp():
            self.client.post("/campaign/%s/test" % camp["id"],
                             data={"to": "owner@example.com"})
        self.assertEqual(len(self.sent), 1)

    def test_an_empty_list_means_no_restriction(self):
        config.ALLOWED_RECIPIENTS = []
        self.assertTrue(config.recipient_allowed("wie.dan.ook@x.nl"))


class ConsentCorrectionTests(Base):
    """Fixing a mistake, without letting a mistake become a way round consent.

    An operator slipping on a button and a customer clicking the link in their
    own email used to be stored identically, so neither could be reversed. The
    `unsubscribed_by` column separates them, and only one of the two is freely
    undoable.
    """

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def _consent(self, email):
        return self.conn.execute(
            "SELECT consent, unsubscribed_by FROM subscriber WHERE email = ?",
            (email,)).fetchone()

    def test_undoing_a_slip_keeps_the_original_provenance(self):
        """Undoing an operator's mistake is not a new consent event. Overwriting
        source would erase the answer to "why may we mail this person at all"."""
        db.upsert_subscriber(self.conn, "bron@x.nl", consent=db.YES, source="shopify")
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM subscriber WHERE email='bron@x.nl'").fetchone()
        self.client.post("/subscribers/%s/unsubscribe" % row["id"])
        self.client.post("/subscribers/%s/restore" % row["id"])
        after = self.conn.execute(
            "SELECT consent, source FROM subscriber WHERE email='bron@x.nl'").fetchone()
        self.assertEqual(after["consent"], db.YES)
        self.assertEqual(after["source"], "shopify")

    def test_an_admin_slip_can_be_undone(self):
        s = self.sub("perongeluk@x.nl")
        self.client.post("/subscribers/%s/unsubscribe" % s["id"])
        row = self._consent("perongeluk@x.nl")
        self.assertEqual(row["consent"], db.NO)
        self.assertEqual(row["unsubscribed_by"], db.BY_ADMIN)
        self.client.post("/subscribers/%s/restore" % s["id"])
        self.assertEqual(self._consent("perongeluk@x.nl")["consent"], db.YES)

    def test_a_self_unsubscribe_is_not_undone_by_a_plain_click(self):
        """The whole reason the column exists."""
        s = self.sub("zelf@x.nl")
        camp = self.campaign()
        self.conn.execute("INSERT INTO send (campaign_id, subscriber_id, to_email, token)"
                          " VALUES (?,?,?,?)", (camp["id"], s["id"], s["email"], "SELFTOK"))
        self.conn.commit()
        self.client.post("/u/SELFTOK")                      # they click the link
        row = self._consent("zelf@x.nl")
        self.assertEqual(row["consent"], db.NO)
        self.assertEqual(row["unsubscribed_by"], db.BY_SELF)

        self.client.post("/subscribers/%s/restore" % s["id"])   # no assertion made
        self.assertEqual(self._consent("zelf@x.nl")["consent"], db.NO)

    def test_a_self_unsubscribe_can_be_reversed_when_they_ask(self):
        s = self.sub("terugvraag@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        self.client.post("/subscribers/%s/restore" % s["id"], data={"they_asked": "yes"})
        row = self.conn.execute(
            "SELECT consent, source FROM subscriber WHERE email='terugvraag@x.nl'").fetchone()
        self.assertEqual(row["consent"], db.YES)
        # and the record says why, not just that it happened
        self.assertEqual(row["source"], "asked_to_rejoin")

    def test_consent_can_be_removed_without_marking_an_opt_out(self):
        """For a consent recorded by mistake, e.g. a CSV imported with the box
        ticked. They stop being mailable without being logged as having asked."""
        s = self.sub("pertoeval@x.nl")
        self.client.post("/subscribers/%s/revoke" % s["id"])
        row = self._consent("pertoeval@x.nl")
        self.assertEqual(row["consent"], db.NEVER)
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)

    def test_a_restored_person_can_be_mailed_again(self):
        s = self.sub("weerterug@x.nl")
        self.client.post("/subscribers/%s/unsubscribe" % s["id"])
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)
        self.client.post("/subscribers/%s/restore" % s["id"])
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)

    def test_an_import_still_cannot_resurrect_a_restored_then_removed_person(self):
        """restore_consent must not weaken the import rule."""
        s = self.sub("blijfweg@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        outcome, _ = db.upsert_subscriber(self.conn, "blijfweg@x.nl", consent=db.YES,
                                          source="shopify")
        self.conn.commit()
        self.assertEqual(outcome, "kept_unsubscribed")
        self.assertEqual(self._consent("blijfweg@x.nl")["consent"], db.NO)


class GrantAndBounceTests(Base):
    """The two states that used to be dead ends: 'no consent on record', and a
    bounced address."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_a_never_row_can_be_marked_subscribed_when_confirmed(self):
        s = self.sub("balie@x.nl", consent=db.NEVER, source="shopify")
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)
        self.client.post("/subscribers/%s/grant" % s["id"], data={"they_opted_in": "yes"})
        row = self.conn.execute(
            "SELECT consent, source, consent_at FROM subscriber WHERE email='balie@x.nl'").fetchone()
        self.assertEqual(row["consent"], db.YES)
        # a NEW consent event, so the provenance changes to say where it came from
        self.assertEqual(row["source"], "manual")
        self.assertIsNotNone(row["consent_at"])
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)

    def test_granting_without_the_confirmation_changes_nothing(self):
        s = self.sub("stil@x.nl", consent=db.NEVER)
        self.client.post("/subscribers/%s/grant" % s["id"])
        self.assertEqual(self.conn.execute(
            "SELECT consent FROM subscriber WHERE email='stil@x.nl'").fetchone()["consent"],
            db.NEVER)

    def test_grant_is_never_a_way_round_an_unsubscribe(self):
        """Someone who opted out must not be recoverable through the easier
        button. /restore exists for that, with a stricter rule."""
        s = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        self.client.post("/subscribers/%s/grant" % s["id"], data={"they_opted_in": "yes"})
        self.assertEqual(self.conn.execute(
            "SELECT consent FROM subscriber WHERE email='weg@x.nl'").fetchone()["consent"],
            db.NO)

    def test_a_bounce_can_be_cleared_and_puts_them_back_in_the_audience(self):
        s = self.sub("vol@x.nl")
        db.mark_bounced(self.conn, s["id"], "452 mailbox full")
        self.conn.commit()
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)
        self.client.post("/subscribers/%s/unbounce" % s["id"])
        row = self.conn.execute(
            "SELECT bounced, bounce_reason FROM subscriber WHERE email='vol@x.nl'").fetchone()
        self.assertEqual(row["bounced"], 0)
        self.assertEqual(row["bounce_reason"], "")
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)

    def test_clearing_a_bounce_does_not_grant_consent(self):
        """Two independent reasons an address is unmailable. Clearing one must
        not quietly clear the other."""
        s = self.sub("geenja@x.nl", consent=db.NEVER)
        db.mark_bounced(self.conn, s["id"], "550")
        self.conn.commit()
        self.client.post("/subscribers/%s/unbounce" % s["id"])
        self.assertEqual(sender.audience_count(self.conn, "all"), 0)


class RowActionApiTests(Base):
    """The endpoint the buttons use now. The rules must live here, not in the
    browser: a page can be edited by whoever is looking at it."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_it_returns_the_freshly_rendered_row_and_new_totals(self):
        s = self.sub("rij@x.nl")
        r = self.client.post("/api/subscriber/%s/unsubscribe" % s["id"])
        d = r.get_json()
        self.assertTrue(d["ok"])
        # the row comes back rendered by the same partial the list uses
        self.assertIn('data-row="%s"' % s["id"], d["row"])
        self.assertIn("unsubscribed", d["row"])
        self.assertIn("Undo", d["row"])
        self.assertEqual(d["stats"]["mailable"], 0)
        self.assertEqual(d["stats"]["unsubscribed"], 1)

    def test_a_self_unsubscribe_is_refused_over_the_api_too(self):
        s = self.sub("zelfapi@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        r = self.client.post("/api/subscriber/%s/restore" % s["id"])
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()["ok"])
        self.assertEqual(self.conn.execute(
            "SELECT consent FROM subscriber WHERE email='zelfapi@x.nl'").fetchone()["consent"],
            db.NO)
        # and allowed once the assertion is made
        r = self.client.post("/api/subscriber/%s/restore" % s["id"],
                             data={"they_asked": "yes"})
        self.assertTrue(r.get_json()["ok"])

    def test_grant_over_the_api_still_needs_the_confirmation(self):
        s = self.sub("bevestig@x.nl", consent=db.NEVER)
        self.assertEqual(self.client.post(
            "/api/subscriber/%s/grant" % s["id"]).status_code, 400)
        r = self.client.post("/api/subscriber/%s/grant" % s["id"],
                             data={"they_opted_in": "yes"})
        self.assertTrue(r.get_json()["ok"])
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)

    def test_an_unknown_action_or_row_changes_nothing(self):
        s = self.sub("bestaat@x.nl")
        self.assertEqual(self.client.post(
            "/api/subscriber/%s/verzin-iets" % s["id"]).status_code, 400)
        self.assertEqual(self.client.post("/api/subscriber/99999/revoke").status_code, 404)
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)


class LayoutTests(Base):
    """The layouts exist so nobody has to type HTML. These pin the two things
    that go wrong when a person types words into a machine that emits markup:
    their punctuation breaking the email, and an empty box leaving a hole."""

    def test_every_layout_renders_from_its_own_defaults(self):
        for spec in layouts.LAYOUTS:
            html = layouts.render(spec["key"], layouts.defaults(spec["key"]))
            self.assertIn("<table", html, spec["key"])
            self.assertGreater(len(html), 400, spec["key"])

    def test_an_unknown_layout_renders_nothing_rather_than_guessing(self):
        self.assertEqual(layouts.render("verzonnen", {"kop": "x"}), "")
        self.assertEqual(layouts.defaults("verzonnen"), {})

    def test_what_somebody_types_is_escaped(self):
        html = layouts.render("plain", {"tekst": "Bank & Fauteuil <b>nu</b>"})
        self.assertIn("Bank &amp; Fauteuil", html)
        self.assertNotIn("<b>nu</b>", html)

    def test_a_percent_sign_survives(self):
        # The blocks are built with %-formatting, so this is the one character
        # that could raise instead of render.
        html = layouts.render("sale", {"kop": "10% korting", "tekst": "Nu 20%s"})
        self.assertIn("10% korting", html)
        self.assertIn("20%s", html)

    def test_blank_lines_make_paragraphs_and_single_ones_make_breaks(self):
        html = layouts.render("plain", {"tekst": "een\ntwee\n\ndrie"})
        self.assertIn("een<br>twee", html)
        self.assertEqual(html.count("<p style"), 2)

    # The chrome always contains a logo image and a WhatsApp link, so these ask
    # about the specific block rather than about any <img> or <a> on the page.
    CTA = "letter-spacing:0.16em"   # only the square uppercase button has this

    def test_an_empty_field_removes_its_block(self):
        filled = dict(layouts.defaults("arrivals"))
        filled["afbeelding"] = "https://example.com/foto.jpg"
        self.assertIn("example.com/foto.jpg", layouts.render("arrivals", filled))
        filled["afbeelding"] = ""
        self.assertNotIn("example.com/foto.jpg", layouts.render("arrivals", filled))

    def test_a_button_needs_both_a_label_and_a_destination(self):
        html = layouts.render("plain", {"knop_tekst": "Klik", "knop_link": ""})
        self.assertNotIn(self.CTA, html)
        html = layouts.render("plain", {"knop_tekst": "",
                                        "knop_link": "https://example.com/"})
        self.assertNotIn(self.CTA, html)
        html = layouts.render("plain", {"knop_tekst": "Klik",
                                        "knop_link": "https://example.com/"})
        self.assertIn(self.CTA, html)

    def test_a_link_that_is_not_http_never_reaches_the_email(self):
        for bad in ("javascript:alert(1)", "www.example.com", "data:text/html,x"):
            html = layouts.render("plain", {"knop_tekst": "Klik", "knop_link": bad,
                                            "afbeelding": bad})
            self.assertNotIn(self.CTA, html)
            self.assertNotIn(bad, html)

    def test_the_blue_hero_is_still_the_house_default(self):
        """Only the promotion is allowed its own colours. If a second layout
        ever drifts, this is where it gets noticed."""
        warm = []
        for spec in layouts.LAYOUTS:
            html = layouts.render(spec["key"], layouts.defaults(spec["key"]))
            if "#44596B" not in html:
                warm.append(spec["key"])
        self.assertEqual(warm, ["promo"])

    def test_the_badge_percentage_works_out_the_card_prices(self):
        v = dict(layouts.defaults("promo"))
        v["korting"] = "-20%"
        html = layouts.render("promo", v, [
            {"title": "Bank", "price": "995", "was": "", "image": "",
             "url": "https://example.com/products/x"}])
        self.assertIn("EUR 995", html)          # struck through
        self.assertIn("EUR 796", html)          # what they pay
        self.assertIn("line-through", html)

    def test_a_product_already_on_sale_never_gets_the_discount_on_top(self):
        """The code is scoped to full-price products, so promising the
        percentage on a marked-down item is an offer the checkout refuses."""
        v = dict(layouts.defaults("promo"))
        v["korting"] = "-20%"
        html = layouts.render("promo", v, [
            {"title": "Showroommodel", "price": "795", "was": "995",
             "image": "", "url": "https://example.com/products/x"}])
        self.assertIn("EUR 995", html)          # the shop's own old price
        self.assertIn("EUR 795", html)          # the shop's own new price
        self.assertNotIn("EUR 636", html)       # 20% off 795, which must NOT appear

    def test_a_price_it_cannot_read_is_left_alone(self):
        v = dict(layouts.defaults("promo"))
        v["korting"] = "-20%"
        html = layouts.render("promo", v, [
            {"title": "Op aanvraag", "price": "", "was": "", "image": "",
             "url": "https://example.com/products/x"}])
        self.assertNotIn("line-through", html)

    def test_a_nonsense_discount_does_not_invent_a_price(self):
        for bad in ("", "veel", "-0%", "-120%"):
            v = dict(layouts.defaults("promo"))
            v["korting"] = bad
            html = layouts.render("promo", v, [
                {"title": "Bank", "price": "995", "was": "", "image": "",
                 "url": "https://example.com/products/x"}])
            self.assertNotIn("line-through", html, bad)

    def test_the_stars_are_real_stars_in_a_font_that_has_them(self):
        """Asterisks read as a footnote, not a rating. And Montserrat has no
        star glyph, so the line asks for Arial first: a missing glyph is an
        empty box on somebody's phone."""
        v = dict(layouts.defaults("promo"))
        v["quote"] = "Prima geholpen in de showroom."   # the block ships empty
        html = layouts.render("promo", v)
        star = chr(0x2605)
        self.assertIn(star * 5, html)
        self.assertNotIn("* * * * *", html)
        at = html.index(star * 5)
        line = html[max(0, at - 200):at]
        self.assertIn("Arial,Helvetica", line)
        self.assertNotIn("Montserrat", line.rsplit("<p ", 1)[-1])

    def test_a_promotion_leaves_a_marker_not_the_word_auto(self):
        """AUTO is an instruction, not a code. A customer must never see it."""
        html = layouts.render("promo", layouts.defaults("promo"))
        self.assertNotIn(">AUTO<", html)
        self.assertIn(layouts.CODE_MARK, html)

    def test_a_campaign_with_a_marker_gets_a_code_per_person(self):
        camp = self.campaign(body="Uw code: " + layouts.CODE_MARK)
        self.assertTrue(sender.needs_code(camp))
        html, _text = sender.render(camp, "Sam", "TOK", code="ELA20-AB12CD")
        self.assertIn("ELA20-AB12CD", html)
        self.assertNotIn(layouts.CODE_MARK, html)

    def test_a_preview_shows_the_shape_of_a_code_never_a_real_one(self):
        camp = self.campaign(body="Uw code: " + layouts.CODE_MARK)
        html, _text = sender.render(camp, "Sam", "TOK")
        self.assertIn(sender.discounts.SAMPLE, html)

    def test_only_the_offer_layout_offers_the_orange_box(self):
        self.assertIn("actie_code", layouts.defaults("sale"))
        for key in ("arrivals", "showroom", "plain"):
            self.assertNotIn("actie_code", layouts.defaults(key), key)

    def test_the_offer_layout_keeps_its_own_wording(self):
        # The orange box goes into the middle of the field list, which is
        # exactly the sort of edit that quietly drops the defaults after it.
        # Checks that every box after it still has its own words, without
        # naming them, so rewriting the copy does not break this.
        d = layouts.defaults("sale")
        spec = next(c for c in mailcopy.LAYOUTS if c["key"] == "sale")
        for key in ("tekst", "knop_tekst", "knop_link", "usp"):
            self.assertTrue(d[key], key)
        self.assertEqual(d["tekst"], spec["tekst"])
        self.assertEqual(d["knop_tekst"], spec["knop_tekst"])

    def test_the_name_placeholder_survives_into_the_body(self):
        # Escaping must not eat it, or personalisation silently stops working.
        html = layouts.render("plain", layouts.defaults("plain"))
        self.assertIn("{{naam}}", html)

    def test_collect_takes_only_the_fields_the_layout_declares(self):
        got = layouts.collect("plain", {"f_tekst": "hallo", "f_verzonnen": "x",
                                        "body": "<script>"})
        self.assertEqual(got["tekst"], "hallo")
        self.assertNotIn("verzonnen", got)
        self.assertNotIn("body", got)


class ComposerTests(Base):
    """The composer end to end: pick a layout, type words, get an email."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def make(self, **fields):
        data = {"name": "Herfst", "subject": "Onderwerp", "audience": "all",
                "layout": "arrivals"}
        data.update({"f_" + k: v for k, v in fields.items()})
        self.client.post("/campaign/new", data=data)
        return self.conn.execute(
            "SELECT * FROM campaign WHERE name='Herfst'").fetchone()

    def test_the_subject_label_agrees_with_the_subject_box(self):
        """It read '(no subject)' beside a filled-in box, which looks like the
        page has not saved what you typed."""
        page = self.client.get("/campaign/new?start=showroom").get_data(as_text=True)
        subj = layouts.get("showroom")["subject"]
        self.assertIn(subj, page)
        self.assertNotIn("(no subject)", page)

    def test_a_layout_with_no_subject_still_says_so(self):
        page = self.client.get("/campaign/new?start=plain").get_data(as_text=True)
        self.assertEqual(layouts.get("plain")["subject"], "")
        self.assertIn("(no subject)", page)

    def test_choosing_a_layout_opens_the_boxes_not_a_html_editor(self):
        page = self.client.get("/campaign/new?start=arrivals").get_data(as_text=True)
        self.assertIn('name="f_kop"', page)
        self.assertIn('name="layout"', page)
        self.assertNotIn('name="body"', page)

    def test_writing_a_campaign_stores_the_words_and_the_rendered_email(self):
        row = self.make(kop="Nieuw binnen", tekst="Kom kijken.",
                        knop_tekst="Bekijk", knop_link="https://example.com/x")
        self.assertEqual(row["layout"], "arrivals")
        self.assertIn("Kom kijken.", row["content"])   # the words, kept as typed
        self.assertIn("Nieuw binnen", row["body"])     # the email, generated
        self.assertIn("https://example.com/x", row["body"])

    def test_editing_the_words_regenerates_the_email(self):
        row = self.make(kop="Eerst", tekst="Een.")
        self.client.post("/campaign/%s" % row["id"], data={
            "name": "Herfst", "subject": "Onderwerp", "audience": "all",
            "f_kop": "Daarna", "f_tekst": "Twee."})
        after = self.conn.execute("SELECT * FROM campaign WHERE id = ?",
                                  (row["id"],)).fetchone()
        self.assertIn("Daarna", after["body"])
        self.assertNotIn("Eerst", after["body"])

    def test_the_layout_cannot_be_switched_by_posting_a_different_one(self):
        # It decides how the words are interpreted, so it is fixed at creation.
        row = self.make(kop="Nieuw binnen")
        self.client.post("/campaign/%s" % row["id"], data={
            "name": "Herfst", "subject": "Onderwerp", "audience": "all",
            "layout": "showroom", "f_kop": "Nieuw binnen"})
        after = self.conn.execute("SELECT layout FROM campaign WHERE id = ?",
                                  (row["id"],)).fetchone()
        self.assertEqual(after["layout"], "arrivals")

    def test_a_campaign_written_before_layouts_still_opens_and_keeps_its_html(self):
        old = self.campaign(body="<p>Met de hand geschreven</p>")
        page = self.client.get("/campaign/%s" % old["id"]).get_data(as_text=True)
        self.assertEqual(old["layout"], "")
        self.assertIn('name="body"', page)
        self.assertIn("Met de hand geschreven", page)

    def test_the_preview_renders_the_fields_through_the_same_code_as_a_send(self):
        r = self.client.post("/api/preview", json={
            "layout": "arrivals", "values": {"kop": "Voorbeeldkop"}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Voorbeeldkop", r.get_data(as_text=True))

    def test_the_preview_ignores_values_that_are_not_text(self):
        r = self.client.post("/api/preview", json={
            "layout": "arrivals", "values": {"kop": ["niet", "tekst"]}})
        self.assertEqual(r.status_code, 200)


def _fake_product(handle, title="Example Sofa", price="1495.00", was=None):
    return {"product": {
        "title": title,
        "image": {"src": "https://cdn.shopify.com/x/%s.webp" % handle},
        "variants": [{"price": price, "compare_at_price": was}],
    }}


class BulkTests(Base):
    """Bulk consent changes. The point of these is what bulk may NOT do."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def consent_of(self, email):
        return self.conn.execute("SELECT * FROM subscriber WHERE email = ?",
                                 (email,)).fetchone()

    def test_a_selection_can_be_unsubscribed_at_once(self):
        a, b, c = (self.sub("a@x.nl"), self.sub("b@x.nl"), self.sub("c@x.nl"))
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "ids": [str(a["id"]), str(b["id"])]})
        self.assertEqual(self.consent_of("a@x.nl")["consent"], db.NO)
        self.assertEqual(self.consent_of("b@x.nl")["consent"], db.NO)
        self.assertEqual(self.consent_of("c@x.nl")["consent"], db.YES)

    def test_bulk_unsubscribes_are_recorded_as_done_from_the_screen(self):
        """So a slip on a hundred rows can still be undone. Recording them as
        'they asked' would be a lie and would make them permanent."""
        a = self.sub("a@x.nl")
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "ids": [str(a["id"])]})
        self.assertEqual(self.consent_of("a@x.nl")["unsubscribed_by"], db.BY_ADMIN)

    def test_a_bulk_unsubscribe_can_be_undone_in_one_go(self):
        """The reason bulk unsubscribe is safe. 'Undo it row by row' is true for
        five people and hollow for a hundred."""
        ids = [self.sub("p%s@x.nl" % i)["id"] for i in range(3)]
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "ids": [str(i) for i in ids]})
        for i in range(3):
            self.assertEqual(self.consent_of("p%s@x.nl" % i)["consent"], db.NO)

        self.client.post("/subscribers/bulk", data={
            "action": "restore", "ids": [str(i) for i in ids]})
        for i in range(3):
            row = self.consent_of("p%s@x.nl" % i)
            self.assertEqual(row["consent"], db.YES, i)
            # Correcting a record, not a new signup: where they came from stands.
            self.assertNotEqual(row["source"], "asked_to_rejoin", i)

    def test_bulk_undo_leaves_alone_anyone_who_unsubscribed_themselves(self):
        """In bulk you cannot honestly claim that N separate people each asked
        to come back. That stays a single-row decision."""
        mine = self.sub("slip@x.nl")
        theirs = self.sub("zelf@x.nl")
        db.unsubscribe(self.conn, mine["id"], by=db.BY_ADMIN)
        db.unsubscribe(self.conn, theirs["id"], by=db.BY_SELF)
        self.conn.commit()

        r = self.client.post("/subscribers/bulk", data={
            "action": "restore",
            "ids": [str(mine["id"]), str(theirs["id"])]}, follow_redirects=True)
        self.assertEqual(self.consent_of("slip@x.nl")["consent"], db.YES)
        self.assertEqual(self.consent_of("zelf@x.nl")["consent"], db.NO)
        self.assertIn("left alone because they unsubscribed themselves",
                      r.get_data(as_text=True))

    def test_bulk_undo_over_a_whole_filter_still_respects_that(self):
        theirs = self.sub("zelf@x.nl")
        db.unsubscribe(self.conn, theirs["id"], by=db.BY_SELF)
        mine = self.sub("slip@x.nl")
        db.unsubscribe(self.conn, mine["id"], by=db.BY_ADMIN)
        self.conn.commit()
        self.client.post("/subscribers/bulk", data={
            "action": "restore", "all_matching": "1", "q": "@x.nl"})
        self.assertEqual(self.consent_of("zelf@x.nl")["consent"], db.NO)
        self.assertEqual(self.consent_of("slip@x.nl")["consent"], db.YES)

    def test_only_the_four_intended_actions_are_allowed(self):
        s = self.sub("stil@x.nl", consent=db.NEVER)
        for action in ("unbounce", "verzin-iets", ""):
            r = self.client.post("/subscribers/bulk",
                                 data={"action": action, "ids": [str(s["id"])]})
            self.assertEqual(r.status_code, 400, action)
        self.assertEqual(self.consent_of("stil@x.nl")["consent"], db.NEVER)

    def test_bulk_grant_refuses_without_confirming_they_opted_in(self):
        """Added on the owner's instruction, but it still has to be a statement
        rather than a click: this is the same confirmation the single row asks
        for, and without it consent could be given by accident."""
        s = self.sub("stil@x.nl", consent=db.NEVER)
        r = self.client.post("/subscribers/bulk",
                             data={"action": "grant", "ids": [str(s["id"])]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.consent_of("stil@x.nl")["consent"], db.NEVER)

        r = self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "nee", "ids": [str(s["id"])]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.consent_of("stil@x.nl")["consent"], db.NEVER)

    def test_bulk_grant_marks_a_selection_and_records_it_as_manual(self):
        a, b = self.sub("a@x.nl", consent=db.NEVER), self.sub("b@x.nl", consent=db.NEVER)
        self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes",
            "ids": [str(a["id"]), str(b["id"])]})
        for e in ("a@x.nl", "b@x.nl"):
            row = self.consent_of(e)
            self.assertEqual(row["consent"], db.YES, e)
            # So a later audit can tell an offline opt-in from a shop signup.
            self.assertEqual(row["source"], "manual", e)

    def test_bulk_grant_is_never_a_route_around_an_unsubscribe(self):
        """The rule that must survive every convenience added to this screen."""
        for by in (db.BY_SELF, db.BY_ADMIN):
            email = "weg-%s@x.nl" % by
            s = self.sub(email)
            db.unsubscribe(self.conn, s["id"], by=by)
            self.conn.commit()
            self.client.post("/subscribers/bulk", data={
                "action": "grant", "they_opted_in": "yes", "ids": [str(s["id"])]})
            row = self.consent_of(email)
            self.assertEqual(row["consent"], db.NO, by)
            self.assertEqual(row["unsubscribed_by"], by)

    def test_bulk_grant_over_a_whole_filter_still_skips_the_unsubscribed(self):
        keep = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, keep["id"], by=db.BY_SELF)
        self.sub("stil@x.nl", consent=db.NEVER)
        self.conn.commit()
        self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes", "all_matching": "1",
            "q": "@x.nl"})
        self.assertEqual(self.consent_of("weg@x.nl")["consent"], db.NO)
        self.assertEqual(self.consent_of("stil@x.nl")["consent"], db.YES)

    def test_the_result_says_how_many_were_left_alone(self):
        s = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        r = self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes", "ids": [str(s["id"])]},
            follow_redirects=True)
        page = r.get_data(as_text=True)
        self.assertIn("left untouched because they unsubscribed", page)

    def test_bulk_can_only_remove_consent_from_someone_who_has_it(self):
        out = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, out["id"], by=db.BY_SELF)
        self.conn.commit()
        self.client.post("/subscribers/bulk", data={
            "action": "revoke", "ids": [str(out["id"])]})
        # Still unsubscribed-by-self, not quietly downgraded to "no consent",
        # which would lose the record that they asked to leave.
        row = self.consent_of("weg@x.nl")
        self.assertEqual(row["consent"], db.NO)
        self.assertEqual(row["unsubscribed_by"], db.BY_SELF)

    def test_all_matching_uses_the_same_query_as_the_list(self):
        for i in range(4):
            self.sub("ja%s@x.nl" % i)
        self.sub("nee@y.nl")
        # Filter to the four, the way the screen would.
        r = self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "all_matching": "1", "q": "@x.nl"})
        self.assertEqual(r.status_code, 302)
        for i in range(4):
            self.assertEqual(self.consent_of("ja%s@x.nl" % i)["consent"], db.NO)
        self.assertEqual(self.consent_of("nee@y.nl")["consent"], db.YES)

    def test_all_matching_ignores_any_ids_the_browser_sent(self):
        """The filter is the authority, not a posted list that could disagree
        with what was on screen."""
        keep = self.sub("blijf@y.nl")
        self.sub("weg@x.nl")
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "all_matching": "1", "q": "@x.nl",
            "ids": [str(keep["id"])]})
        self.assertEqual(self.consent_of("blijf@y.nl")["consent"], db.YES)
        self.assertEqual(self.consent_of("weg@x.nl")["consent"], db.NO)

    def test_rubbish_ids_change_nothing(self):
        s = self.sub("a@x.nl")
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "ids": ["nope", "-1", "99999", ""]})
        self.assertEqual(self.consent_of("a@x.nl")["consent"], db.YES)

    def test_the_page_offers_the_boxes_and_the_two_safe_actions(self):
        self.sub("a@x.nl")
        page = self.client.get("/subscribers").get_data(as_text=True)
        self.assertIn('class="rowpick"', page)
        self.assertIn('id="pickall"', page)
        self.assertIn('data-bulk="unsubscribe"', page)
        self.assertIn('data-bulk="revoke"', page)
        self.assertIn('data-bulk="grant"', page)
        self.assertIn('data-bulk="restore"', page)
        self.assertIn('name="they_opted_in"', page)
        self.assertNotIn('data-bulk="unbounce"', page)


class SpendColumnTests(Base):
    """Signup date and total spend. Never opens a real connection: the CRM
    lookup is replaced, so neither a database outage nor a missing driver can
    turn this suite red."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_a_signup_date_is_kept_apart_from_the_date_we_imported(self):
        """Importing 400 people does not mean 400 people signed up today."""
        db.upsert_subscriber(self.conn, "koper@x.nl", consent=db.YES,
                             source="shopify", signed_up_at="2026-07-14")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email='koper@x.nl'").fetchone()
        self.assertEqual(row["signed_up_at"], "2026-07-14")
        self.assertNotEqual(row["signed_up_at"], row["created"][:10])

    def test_a_signup_date_never_overwrites_one_we_already_have(self):
        db.upsert_subscriber(self.conn, "a@x.nl", consent=db.YES,
                             signed_up_at="2026-07-14")
        db.upsert_subscriber(self.conn, "a@x.nl", consent=db.YES,
                             signed_up_at="2026-08-30")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT signed_up_at FROM subscriber WHERE email='a@x.nl'").fetchone()
        self.assertEqual(row["signed_up_at"], "2026-07-14")

    def test_learning_a_signup_date_is_not_a_consent_change(self):
        """It gets filled in even for somebody who has unsubscribed, and that
        must not disturb the one rule this file exists to protect."""
        s = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        outcome, _ = db.upsert_subscriber(self.conn, "weg@x.nl", consent=db.YES,
                                          signed_up_at="2026-07-14")
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email='weg@x.nl'").fetchone()
        self.assertEqual(outcome, "kept_unsubscribed")
        self.assertEqual(row["consent"], db.NO)
        self.assertEqual(row["signed_up_at"], "2026-07-14")

    def test_sync_copies_spend_onto_the_people_we_have(self):
        self.sub("koper@x.nl")
        self.sub("nooit@x.nl")
        fake = {"koper@x.nl": (1495.0, 2, "2024-03-11", "2025-08-02")}
        with mock.patch.object(crm, "spend_by_email", return_value=fake):
            result = crm.sync(self.conn)
        self.assertEqual(result["matched"], 1)
        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email='koper@x.nl'").fetchone()
        self.assertEqual(row["spent"], 1495.0)
        self.assertEqual(row["orders"], 2)
        self.assertEqual(row["first_order_at"], "2024-03-11")
        # Recency is what the audiences are built on, so it has to arrive.
        self.assertEqual(row["last_order_at"], "2025-08-02")
        other = self.conn.execute(
            "SELECT spent, orders FROM subscriber WHERE email='nooit@x.nl'").fetchone()
        self.assertEqual((other["spent"], other["orders"]), (0, 0))

    def test_sync_never_changes_consent(self):
        """It reads one system and writes three numbers. Nothing else."""
        s = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        with mock.patch.object(crm, "spend_by_email",
                               return_value={"weg@x.nl": (999.0, 1, "2025-01-01", "2025-01-01")}):
            crm.sync(self.conn)
        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email='weg@x.nl'").fetchone()
        self.assertEqual(row["consent"], db.NO)
        self.assertEqual(row["unsubscribed_by"], db.BY_SELF)
        self.assertEqual(row["spent"], 999.0)

    def test_a_crm_that_is_down_leaves_the_page_working(self):
        self.sub("a@x.nl")
        with mock.patch.object(crm, "configured", return_value=True), \
             mock.patch.object(crm, "sync", side_effect=OSError("no route to host")):
            r = self.client.post("/subscribers/sync/crm", follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Could not reach the CRM", r.get_data(as_text=True))

    def test_without_a_configured_crm_it_says_so_rather_than_failing(self):
        with mock.patch.object(crm, "configured", return_value=False):
            r = self.client.post("/subscribers/sync/crm", follow_redirects=True)
        self.assertIn("No CRM connection configured", r.get_data(as_text=True))

    def test_the_list_shows_both_columns(self):
        self.sub("koper@x.nl")
        with mock.patch.object(crm, "spend_by_email",
                               return_value={"koper@x.nl": (1495.0, 2, "2024-03-11", "2025-08-02")}):
            crm.sync(self.conn)
        page = self.client.get("/subscribers").get_data(as_text=True)
        self.assertIn("Signed up", page)
        self.assertIn("Spent", page)
        self.assertIn("EUR 1.495", page)

    def test_an_import_date_is_shown_as_uncertain_not_as_a_signup_date(self):
        self.sub("onbekend@x.nl")            # no signed_up_at
        page = self.client.get("/subscribers").get_data(as_text=True)
        self.assertIn("not when they signed up", page)

    def test_the_query_only_counts_real_orders(self):
        """Proforma, cancelled and rows without a total would each inflate what
        a customer appears to have spent."""
        sql = crm.SPEND_SQL.lower()
        self.assertIn("s.kind = 'order'", sql)
        self.assertIn("s.status <> 'cancelled'", sql)
        self.assertIn("total_override is not null", sql)

    def test_the_crm_is_only_ever_read(self):
        sql = crm.SPEND_SQL.lower()
        for writing in ("insert", "update", "delete", "drop", "alter", "truncate"):
            self.assertNotIn(writing, sql)


class CrmImportTests(Base):
    """Importing the CRM's customers. The CRM holds 5.900 addresses and 184
    newsletter ticks, so what this must NOT do is the whole point."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    FAKE = [
        ("tikte@x.nl", "Sam Yilmaz", True, "2019-04-02"),
        ("kocht@x.nl", "Mehmet Demir", False, "2015-11-20"),
        ("ook.niet@x.nl", "Jan Jansen", False, "2023-06-01"),
    ]

    def imported(self):
        with mock.patch.object(crm, "customers", return_value=self.FAKE):
            return crm.import_customers(self.conn)

    def row(self, email):
        return self.conn.execute("SELECT * FROM subscriber WHERE email = ?",
                                 (email,)).fetchone()

    def test_only_the_newsletter_tick_becomes_consent(self):
        """An address is not consent. 5.760 CRM customers have one and never
        asked for anything."""
        tally = self.imported()
        self.assertEqual(tally["seen"], 3)
        self.assertEqual(tally["with_consent"], 1)
        self.assertEqual(self.row("tikte@x.nl")["consent"], db.YES)
        self.assertEqual(self.row("kocht@x.nl")["consent"], db.NEVER)
        self.assertEqual(self.row("ook.niet@x.nl")["consent"], db.NEVER)
        self.assertEqual(sender.audience_count(self.conn, "all"), 1)

    def test_it_brings_the_real_start_date(self):
        """The one thing the CRM has that Shopify does not: history back to
        2013 rather than the date of a migration."""
        self.imported()
        self.assertEqual(self.row("kocht@x.nl")["signed_up_at"], "2015-11-20")

    def test_it_can_never_resurrect_an_unsubscribe(self):
        s = self.sub("tikte@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        self.imported()
        row = self.row("tikte@x.nl")
        self.assertEqual(row["consent"], db.NO)
        self.assertEqual(row["unsubscribed_by"], db.BY_SELF)

    def test_running_it_twice_adds_nobody_twice(self):
        self.imported()
        again = self.imported()
        self.assertEqual(again["created"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0], 3)

    def test_one_person_with_several_customer_records_is_one_subscriber(self):
        sql = crm.CUSTOMERS_SQL.lower()
        self.assertIn("group by 1", sql)
        self.assertIn("bool_or(newsletter)", sql)   # any tick counts as a tick

    def test_the_customer_query_only_reads(self):
        sql = crm.CUSTOMERS_SQL.lower()
        for writing in ("insert", "update", "delete", "drop", "alter", "truncate"):
            self.assertNotIn(writing, sql)

    def test_the_list_puts_the_newest_people_first(self):
        self.imported()
        page = self.client.get("/subscribers").get_data(as_text=True)
        order = [page.index(e) for e in
                 ("ook.niet@x.nl", "tikte@x.nl", "kocht@x.nl")]  # 2023, 2019, 2015
        self.assertEqual(order, sorted(order), "newest signup should be at the top")

    def test_somebody_with_no_signup_date_still_lands_sensibly(self):
        self.imported()
        self.sub("vandaag@x.nl")            # no signed_up_at at all
        page = self.client.get("/subscribers").get_data(as_text=True)
        # Falls back to when we first saw them, which is today, so it goes top.
        self.assertLess(page.index("vandaag@x.nl"), page.index("kocht@x.nl"))


class BuyerFilterTests(Base):
    """Selecting past buyers, and only them.

    The owner's basis for mailing this group is that they bought and gave
    consent at the counter, which the old CRM never recorded. That argument
    covers customers and nobody else, so the filter has to be exact."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def buyer(self, email, spent, consent=db.NEVER):
        s = self.sub(email, consent=consent)
        self.conn.execute("UPDATE subscriber SET spent = ?, orders = 1 WHERE id = ?",
                          (spent, s["id"]))
        self.conn.commit()
        return s

    def row(self, email):
        return self.conn.execute("SELECT * FROM subscriber WHERE email = ?",
                                 (email,)).fetchone()

    def test_the_filter_separates_buyers_from_everybody_else(self):
        self.buyer("kocht@x.nl", 1495.0)
        self.sub("nooit@x.nl", consent=db.NEVER)
        page = self.client.get("/subscribers?spend=yes").get_data(as_text=True)
        self.assertIn("kocht@x.nl", page)
        self.assertNotIn("nooit@x.nl", page)
        page = self.client.get("/subscribers?spend=no").get_data(as_text=True)
        self.assertIn("nooit@x.nl", page)
        self.assertNotIn("kocht@x.nl", page)

    def test_marking_the_buyers_reaches_them_and_nobody_else(self):
        self.buyer("kocht@x.nl", 1495.0)
        self.sub("nooit@x.nl", consent=db.NEVER)
        self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes",
            "all_matching": "1", "spend": "yes"})
        self.assertEqual(self.row("kocht@x.nl")["consent"], db.YES)
        self.assertEqual(self.row("nooit@x.nl")["consent"], db.NEVER)

    def test_consent_from_a_purchase_is_recorded_as_such(self):
        """In a year nobody will remember which grants rested on a purchase and
        which were typed in, unless it is written down now."""
        self.buyer("kocht@x.nl", 1495.0)
        self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes",
            "all_matching": "1", "spend": "yes"})
        self.assertEqual(self.row("kocht@x.nl")["source"], "customer")

    def test_a_grant_without_that_basis_is_still_recorded_as_manual(self):
        s = self.sub("stil@x.nl", consent=db.NEVER)
        self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes", "ids": [str(s["id"])]})
        self.assertEqual(self.row("stil@x.nl")["source"], "manual")

    def test_a_buyer_who_unsubscribed_is_still_left_alone(self):
        """Having bought something is not a way back onto the list."""
        s = self.buyer("weg@x.nl", 2000.0, consent=db.YES)
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        self.client.post("/subscribers/bulk", data={
            "action": "grant", "they_opted_in": "yes",
            "all_matching": "1", "spend": "yes"})
        self.assertEqual(self.row("weg@x.nl")["consent"], db.NO)

    def test_somebody_who_ordered_but_spent_nothing_is_not_a_buyer(self):
        s = self.sub("gratis@x.nl", consent=db.NEVER)
        self.conn.execute("UPDATE subscriber SET spent = 0, orders = 1 WHERE id = ?",
                          (s["id"],))
        self.conn.commit()
        page = self.client.get("/subscribers?spend=yes").get_data(as_text=True)
        self.assertNotIn("gratis@x.nl", page)


class ColumnFilterTests(Base):
    """Sorting and filtering from the table header."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        rows = [("aap@x.nl", "Aap", 0.0, "crm", "2019-04-02"),
                ("noot@x.nl", "Noot", 2500.0, "customer", "2024-07-11"),
                ("mies@x.nl", "Mies", 700.0, "shopify", "2026-02-20")]
        for email, name, spent, source, signed in rows:
            s = self.sub(email)
            self.conn.execute(
                "UPDATE subscriber SET name=?, spent=?, orders=?, source=?,"
                " signed_up_at=? WHERE id=?",
                (name, spent, 1 if spent else 0, source, signed, s["id"]))
        self.conn.commit()

    def emails(self, url):
        page = self.client.get(url).get_data(as_text=True)
        return [e for e in ("aap@x.nl", "noot@x.nl", "mies@x.nl")
                if e in page]

    def order(self, url):
        page = self.client.get(url).get_data(as_text=True)
        found = [(page.index(e), e) for e in ("aap@x.nl", "noot@x.nl", "mies@x.nl")
                 if e in page]
        return [e for _i, e in sorted(found)]

    def test_a_made_up_sort_column_is_ignored_not_run(self):
        """ORDER BY cannot be parameterised, so anything not on the whitelist
        has to be dropped rather than passed to the database."""
        for bad in ("id; DROP TABLE subscriber", "spent--", "(SELECT 1)", "nope"):
            r = self.client.get("/subscribers?sort=" + bad)
            self.assertEqual(r.status_code, 200, bad)
        # The table is still there and still has everyone in it.
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0], 3)

    def test_sorting_by_spend_works_both_ways(self):
        self.assertEqual(self.order("/subscribers?sort=spent&dir=desc")[0],
                         "noot@x.nl")
        self.assertEqual(self.order("/subscribers?sort=spent&dir=asc")[0],
                         "aap@x.nl")

    def test_sorting_by_name(self):
        self.assertEqual(self.order("/subscribers?sort=name&dir=asc"),
                         ["aap@x.nl", "mies@x.nl", "noot@x.nl"])

    def test_the_default_is_newest_signup_first(self):
        self.assertEqual(self.order("/subscribers"),
                         ["mies@x.nl", "noot@x.nl", "aap@x.nl"])

    def test_filtering_by_source(self):
        self.assertEqual(self.emails("/subscribers?source=shopify"), ["mies@x.nl"])

    def test_filtering_by_year_signed_up(self):
        self.assertEqual(self.emails("/subscribers?year=2019"), ["aap@x.nl"])

    def test_filtering_by_spend_band(self):
        self.assertEqual(sorted(self.emails("/subscribers?spend=1000")),
                         ["noot@x.nl"])
        self.assertEqual(sorted(self.emails("/subscribers?spend=500")),
                         ["mies@x.nl", "noot@x.nl"])

    def test_a_nonsense_spend_filter_is_dropped_rather_than_crashing(self):
        r = self.client.get("/subscribers?spend=abc")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(self.emails("/subscribers?spend=abc")), 3)

    def test_filters_combine(self):
        self.assertEqual(self.emails("/subscribers?source=customer&spend=1000"),
                         ["noot@x.nl"])
        self.assertEqual(self.emails("/subscribers?source=shopify&spend=1000"), [])

    def test_no_table_header_anywhere_is_sticky(self):
        """The cause of a bug I chased on three separate screens.

        `.tbl-wrap` sets `overflow-x: auto`, which makes it a scroll container.
        A sticky element inside a scroll container positions against THAT
        container, not the page, so `top: 52px` pushed every table header 52px
        down from the card's own top edge and left a white band above it. It
        looked like a layout fault on whichever screen was being looked at.
        Every table in the app sits in that wrapper, so the rule has to stay
        gone rather than be overridden per screen."""
        css = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "style.css")
        with open(css, encoding="utf-8") as fh:
            rules = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
        for line in rules.split("\n"):
            stripped = line.strip()
            if stripped.startswith("th ") or stripped.startswith("th{") \
               or stripped.startswith("th,") or " th {" in stripped:
                self.assertNotIn("position: sticky", stripped, stripped)

    def test_changing_position_on_a_header_cell_also_clears_the_offset(self):
        """The global th rule sets `position: sticky` AND `top: 52px`. Override
        only the position and the offset stays behind: `position: relative` then
        means "draw yourself 52px below your own slot", which put the heading
        row underneath the filter row and left a blank band where it belonged.
        Three separate attempts missed this because the position looked right."""
        css = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "style.css")
        with open(css, encoding="utf-8") as fh:
            rules = re.sub(r"/\*.*?\*/", "", fh.read(), flags=re.S)
        for line in rules.split("\n"):
            if "th {" in line and "position:" in line and "sticky" not in line:
                self.assertIn("top:", line,
                              "changes position without clearing top: " + line.strip())

    def test_every_column_is_addressable_in_all_three_rows(self):
        """Show/hide and resize both work by data-col, so a column missing the
        attribute in one row would half-disappear when toggled."""
        page = self.client.get("/subscribers").get_data(as_text=True)
        cols = ["email", "name", "consent", "source", "signed_up", "spent",
                "last_mailed"]
        heads = re.search(r'<tr class="cols">(.*?)</tr>', page, re.S).group(1)
        filts = re.search(r'<tr class="filters">(.*?)</tr>', page, re.S).group(1)
        row = re.search(r"<tbody>.*?(<tr\b.*?</tr>)", page, re.S).group(1)
        for col in cols:
            for where, name in ((heads, "heading"), (filts, "filter"), (row, "data")):
                self.assertIn('data-col="%s"' % col, where, "%s / %s" % (col, name))

    def test_the_column_menu_lists_every_column(self):
        page = self.client.get("/subscribers").get_data(as_text=True)
        for col in ("email", "name", "consent", "source", "signed_up", "spent",
                    "last_mailed"):
            self.assertIn('data-toggle="%s"' % col, page, col)
        self.assertIn('id="colreset"', page)

    def test_the_three_header_and_body_rows_have_the_same_number_of_cells(self):
        page = self.client.get("/subscribers").get_data(as_text=True)
        heads = re.search(r'<tr class="cols">(.*?)</tr>', page, re.S)
        filts = re.search(r'<tr class="filters">(.*?)</tr>', page, re.S)
        # One data row, not the whole body: nine cells times three people is 27.
        body = re.search(r"<tbody>.*?(<tr\b.*?</tr>)", page, re.S)
        n = len(re.findall(r"<th\b", heads.group(1)))
        self.assertEqual(n, len(re.findall(r"<th\b", filts.group(1))))
        self.assertEqual(n, len(re.findall(r"<td\b", body.group(1))))

    def test_no_header_cell_is_taken_out_of_the_table_layout(self):
        """A <th> given a display other than table-cell stops being a cell, the
        browser synthesises an anonymous one, and that row stops lining up with
        the headings. It put half of "Spent" somewhere it did not belong."""
        css = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "style.css")
        with open(css, encoding="utf-8") as fh:
            rules = fh.read()
        # Comments first, or this trips on the note explaining why the rule it
        # is looking for must not exist.
        rules = re.sub(r"/\*.*?\*/", "", rules, flags=re.S)
        for line in rules.split("\n"):
            if " th" in line and "display:" in line:
                self.assertNotIn("display: flex", line, line.strip())
                self.assertNotIn("display: grid", line, line.strip())
                self.assertNotIn("display: block", line, line.strip())

    def test_things_marked_hidden_are_actually_hidden(self):
        """`.bulkbar { display: flex }` beat the browser's own rule for the
        hidden attribute, so the bulk bar sat on screen permanently saying
        "0 selected". The markup was right and the stylesheet undid it."""
        page = self.client.get("/subscribers").get_data(as_text=True)
        self.assertIn('id="bulkbar"', page)
        bar = page[page.index('id="bulkbar"'):]
        self.assertIn("hidden", bar[:bar.index(">")])

        css = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "static", "style.css")
        with open(css, encoding="utf-8") as fh:
            rules = fh.read()
        self.assertIn("[hidden] { display: none !important }", rules)

    def test_the_page_does_not_repeat_what_is_already_on_it(self):
        """The lede listed four numbers, three of which the screen shows
        elsewhere, above a six-line notice shown on every single visit."""
        page = self.client.get("/subscribers").get_data(as_text=True)
        self.assertIn("may be mailed", page)
        self.assertNotIn("unsubscribed &middot;", page)
        self.assertNotIn("An address is not consent", page)

    def test_the_way_out_appears_only_when_something_is_filtering(self):
        plain = self.client.get("/subscribers").get_data(as_text=True)
        self.assertNotIn("Clear every filter", plain)
        filtered = self.client.get("/subscribers?source=crm").get_data(as_text=True)
        self.assertIn("Clear every filter", filtered)

    def test_a_bulk_action_acts_on_the_filtered_set_only(self):
        """The filters are in the header now, so this is the path most likely to
        drift from what is on screen."""
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "all_matching": "1", "source": "shopify"})
        states = dict(self.conn.execute(
            "SELECT email, consent FROM subscriber").fetchall())
        self.assertEqual(states["mies@x.nl"], db.NO)
        self.assertEqual(states["aap@x.nl"], db.YES)
        self.assertEqual(states["noot@x.nl"], db.YES)


class PaginatorTests(Base):
    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        for i in range(120):
            self.sub("p%03d@x.nl" % i)

    def flat(self, url):
        """The page with runs of whitespace collapsed. These assertions are
        about what it says, not about where the template wraps its lines."""
        return " ".join(self.client.get(url).get_data(as_text=True).split())

    def test_it_says_which_rows_you_are_looking_at(self):
        page = self.flat("/subscribers?per=50")
        self.assertIn("<b>1</b> to <b>50</b>", page)
        self.assertIn("of <b>120</b>", page)
        page = self.flat("/subscribers?per=50&page=3")
        self.assertIn("<b>101</b> to <b>120</b>", page)

    def test_you_can_type_a_page_number(self):
        """At 116 pages, Next is not a way to reach page 90."""
        page = self.flat("/subscribers?per=50")
        self.assertIn('name="page" type="number"', page)
        self.assertIn('max="3"', page)
        r = self.client.get("/subscribers?per=50&page=3")
        self.assertIn("<b>101</b> to <b>120</b>",
                      " ".join(r.get_data(as_text=True).split()))

    def test_the_steps_are_icons_rather_than_punctuation(self):
        page = self.flat("/subscribers?per=50")
        for icon in ("i-first", "i-prev", "i-next", "i-last"):
            self.assertIn(icon, page, icon)

    def test_the_paginator_does_not_reuse_a_class_another_screen_owns(self):
        """`.steps` belongs to the campaign page's three-step checklist, and it
        is a grid with a bottom margin. Sharing the name silently dragged those
        into the paginator and pushed the buttons out of the row."""
        page = self.flat("/subscribers?per=50")
        # From the pager's own opening tag, not the first </nav> on the page:
        # the site header has a <nav> too, and it closes long before this one.
        start = page.index('<nav class="pager"')
        nav = page[start:page.index("</nav>", start)]
        self.assertIn('class="pgsteps"', nav)
        self.assertNotIn('class="steps"', nav)

    def test_the_rows_per_page_choice_is_honoured(self):
        for per in (50, 100):
            page = self.client.get("/subscribers?per=%s" % per).get_data(as_text=True)
            self.assertEqual(page.count('class="rowpick"'), per, per)

    def test_a_silly_page_size_falls_back_rather_than_breaking(self):
        for bad in ("0", "-5", "999999", "abc", ""):
            r = self.client.get("/subscribers?per=%s" % bad)
            self.assertEqual(r.status_code, 200, bad)
            self.assertEqual(r.get_data(as_text=True).count('class="rowpick"'), 100)

    def test_the_ends_are_inert_rather_than_missing(self):
        """Buttons that vanish at the ends make the row jump under the cursor."""
        first = self.client.get("/subscribers?per=50").get_data(as_text=True)
        self.assertIn('class="pgbtn off"', first)
        self.assertIn("Next page", first)
        last = self.client.get("/subscribers?per=50&page=3").get_data(as_text=True)
        self.assertIn('class="pgbtn off"', last)
        self.assertIn("Previous page", last)

    def test_paging_keeps_the_filters(self):
        page = self.client.get(
            "/subscribers?per=50&spend=no&state=subscribed").get_data(as_text=True)
        self.assertIn("spend=no", page)
        self.assertIn("state=subscribed", page)
        self.assertIn("per=50", page)


class BounceTests(Base):
    """A dead address has to come off the list the first time it refuses.

    Mailing it again next month and the month after is what turns one bad
    address into a damaged sending domain, and this one also carries the
    webshop's order confirmations."""

    def refuse(self, code, message=b"mailbox unavailable"):
        import smtplib
        return smtplib.SMTPRecipientsRefused({"weg@x.nl": (code, message)})

    def test_a_permanent_refusal_is_a_bounce(self):
        for code in (550, 551, 553, 599):
            permanent, reason = sender.classify_failure(self.refuse(code))
            self.assertTrue(permanent, code)
            self.assertIn(str(code), reason)

    def test_a_temporary_refusal_is_not(self):
        """A full mailbox or a greylist. Removing somebody for that loses a
        real customer for a reason that will be gone in an hour."""
        for code in (421, 450, 451, 452):
            permanent, _ = sender.classify_failure(self.refuse(code))
            self.assertFalse(permanent, code)

    def test_our_own_failures_never_mark_anybody(self):
        for exc in (OSError("no route to host"), ValueError("bad config"),
                    Exception("tls handshake failed")):
            permanent, _ = sender.classify_failure(exc)
            self.assertFalse(permanent, exc)

    def test_a_response_exception_is_read_too(self):
        import smtplib
        permanent, reason = sender.classify_failure(
            smtplib.SMTPResponseException(550, b"no such user"))
        self.assertTrue(permanent)
        self.assertIn("no such user", reason)
        permanent, _ = sender.classify_failure(
            smtplib.SMTPResponseException(451, b"try later"))
        self.assertFalse(permanent)

    def test_a_bounced_address_is_marked_and_never_queued_again(self):
        camp = self.campaign()
        s = self.sub("weg@x.nl")
        self.sub("goed@x.nl")
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                          (db.SENDING, camp["id"]))
        self.conn.commit()

        outer = self

        class Refusing:
            def send_message(self, msg):
                if msg["To"] == "weg@x.nl":
                    raise outer.refuse(550, b"no such user")
                outer.sent.append(msg)

            def quit(self):
                pass

        with mock.patch.object(sender, "_smtp", return_value=Refusing()):
            sender.send_batch(self.conn, camp["id"])

        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email='weg@x.nl'").fetchone()
        self.assertEqual(row["bounced"], 1)
        self.assertIn("550", row["bounce_reason"])
        # And every audience drops them from here on.
        for key, _label in sender.AUDIENCES:
            where, params = sender.audience_sql(key)
            got = {r["email"] for r in self.conn.execute(
                "SELECT email FROM subscriber WHERE " + where, params)}
            self.assertNotIn("weg@x.nl", got, key)

    def test_a_temporary_failure_leaves_them_on_the_list(self):
        camp = self.campaign()
        self.sub("vol@x.nl")
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                          (db.SENDING, camp["id"]))
        self.conn.commit()
        outer = self

        class Full:
            def send_message(self, msg):
                raise outer.refuse(452, b"mailbox full")

            def quit(self):
                pass

        with mock.patch.object(sender, "_smtp", return_value=Full()):
            sender.send_batch(self.conn, camp["id"])
        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email='vol@x.nl'").fetchone()
        self.assertEqual(row["bounced"], 0)


class NumberedListTests(Base):
    """Fixed lists of 275, one a day under the 300 cap.

    Unlike the rule-based lists these have FIXED membership, because the point
    is being able to say next month exactly who was in list 7."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def people(self, n, **kw):
        for i in range(n):
            s = self.sub("p%04d@x.nl" % i, **kw)
            self.conn.execute(
                "UPDATE subscriber SET last_order_at = ? WHERE id = ?",
                ("2026-%02d-01" % (1 + i % 12), s["id"]))
        self.conn.commit()

    def sizes(self):
        return dict(self.conn.execute(
            "SELECT batch, COUNT(*) FROM subscriber WHERE batch > 0"
            " GROUP BY batch ORDER BY batch").fetchall())

    def test_everyone_mailable_gets_a_number_in_lists_of_the_right_size(self):
        self.people(700)
        result = db.build_batches(self.conn, size=275)
        self.assertEqual(result["assigned"], 700)
        self.assertEqual(result["lists"], 3)
        self.assertEqual(self.sizes(), {1: 275, 2: 275, 3: 150})

    def test_a_list_is_created_for_each_number(self):
        self.people(300)
        db.build_batches(self.conn, size=275)
        names = [s["name"] for s in db.segments(self.conn)]
        self.assertIn("Email list 1", names)
        self.assertIn("Email list 2", names)
        where, params = sender.audience_sql_for(self.conn, "list:%s" % [
            s["id"] for s in db.segments(self.conn) if s["name"] == "Email list 1"][0])
        n = self.conn.execute(
            "SELECT COUNT(*) FROM subscriber WHERE " + where, params).fetchone()[0]
        self.assertEqual(n, 275)

    def test_people_who_cannot_be_mailed_get_no_number(self):
        self.people(3)
        stil = self.sub("stil@x.nl", consent=db.NEVER)
        out = self.sub("weg@x.nl")
        db.unsubscribe(self.conn, out["id"], by=db.BY_SELF)
        self.conn.commit()
        db.build_batches(self.conn, size=275)
        for email in ("stil@x.nl", "weg@x.nl"):
            row = self.conn.execute(
                "SELECT batch FROM subscriber WHERE email = ?", (email,)).fetchone()
            self.assertEqual(row["batch"], 0, email)

    def test_running_it_again_appends_rather_than_reshuffling(self):
        """Renumbering after a send would make every record of who received
        what a lie."""
        self.people(300)
        db.build_batches(self.conn, size=275)
        before = dict(self.conn.execute(
            "SELECT email, batch FROM subscriber").fetchall())

        self.people(50)                       # an import brings newcomers
        # give the new ones different addresses so they are genuinely new
        for i in range(50):
            self.sub("nieuw%03d@x.nl" % i)
        self.conn.commit()
        db.build_batches(self.conn, size=275)
        after = dict(self.conn.execute(
            "SELECT email, batch FROM subscriber").fetchall())
        for email, batch in before.items():
            self.assertEqual(after[email], batch, email)

    def test_the_partly_full_list_is_filled_before_a_new_one_opens(self):
        self.people(300)
        db.build_batches(self.conn, size=275)
        self.assertEqual(self.sizes()[2], 25)
        for i in range(100):
            self.sub("later%03d@x.nl" % i)
        self.conn.commit()
        db.build_batches(self.conn, size=275)
        self.assertEqual(self.sizes()[2], 125)

    def test_list_one_holds_the_most_recent_buyers(self):
        """Warm-up order: the people most likely to open go first."""
        for i, last in enumerate(("2020-01-01", "2026-08-01", "2023-01-01")):
            s = self.sub("p%s@x.nl" % i)
            self.conn.execute(
                "UPDATE subscriber SET last_order_at = ?, spent = 100 WHERE id = ?",
                (last, s["id"]))
        self.conn.commit()
        db.build_batches(self.conn, size=1)
        first = self.conn.execute(
            "SELECT email FROM subscriber WHERE batch = 1").fetchone()
        self.assertEqual(first["email"], "p1@x.nl")

    def test_renumbering_is_refused_once_anything_has_been_sent(self):
        self.people(10)
        db.build_batches(self.conn, size=5)
        camp = self.campaign()
        s = self.conn.execute("SELECT id, email FROM subscriber LIMIT 1").fetchone()
        self.conn.execute(
            "INSERT INTO send (campaign_id, subscriber_id, to_email, token, sent,"
            " sent_at) VALUES (?,?,?,?,1,?)",
            (camp["id"], s["id"], s["email"], db.token(), db.now()))
        self.conn.commit()
        before = self.sizes()
        r = self.client.post("/lists/rebuild", follow_redirects=True)
        self.assertIn("Not renumbering", r.get_data(as_text=True))
        self.assertEqual(self.sizes(), before)

    def test_the_lists_are_ordered_the_way_you_read_them(self):
        """Alphabetically, "Email list 10" comes before "Email list 2", which
        put 10, 11 and 12 straight after 1 and defeated the whole point of
        numbering them in sending order."""
        self.people(3000)
        db.build_batches(self.conn, size=275)
        names = [s["name"] for s in db.segments(self.conn)]
        self.assertEqual(names[:4], ["Email list 1", "Email list 2",
                                     "Email list 3", "Email list 4"])
        numbers = [int(n.rsplit(" ", 1)[1]) for n in names]
        self.assertEqual(numbers, sorted(numbers))

    def test_that_order_is_what_the_screens_show(self):
        self.people(3000)          # enough to reach a list 10
        db.build_batches(self.conn, size=275)
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertLess(page.index("Email list 2"), page.index("Email list 10"))
        campaign_page = self.client.get("/campaign/new?start=plain").get_data(
            as_text=True)
        self.assertLess(campaign_page.index("Email list 2"),
                        campaign_page.index("Email list 10"))

    def test_the_button_builds_them(self):
        self.people(30)
        self.client.post("/lists/build", data={"size": "10"})
        self.assertEqual(len(self.sizes()), 3)
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertIn("Email list 1", page)
        self.assertIn("Email list 3", page)


class ListsScreenTests(Base):
    """One screen showing every list, and who is in each.

    Before this the audiences existed only inside a dropdown on a campaign, so
    the only way to find out who was in one was to aim a campaign at it."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        for email, spent in (("koper@x.nl", 1500.0), ("stil@x.nl", 0.0)):
            s = self.sub(email)
            self.conn.execute(
                "UPDATE subscriber SET spent = ?, orders = ?, last_order_at = ?"
                " WHERE id = ?",
                (spent, 1 if spent else 0, "2026-08-01" if spent else "", s["id"]))
        self.conn.commit()

    def test_the_screen_lists_every_audience_with_its_size(self):
        page = self.client.get("/lists").get_data(as_text=True)
        for _key, label in sender.AUDIENCES:
            self.assertIn(label, page, label)
        self.assertIn("See who is in it", page)

    def test_saved_lists_appear_there_too(self):
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "spend": "yes"})
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertIn("Kopers", page)

    def campaign_to(self, audience, name="Herfst", sent=0):
        cur = self.conn.execute(
            "INSERT INTO campaign (name, subject, body, audience, status,"
            " created, started) VALUES (?,'S','<p>x</p>',?,?,?,?)",
            (name, audience, db.SENT, db.now(), db.now()))
        cid = cur.lastrowid
        for i in range(sent):
            s = self.sub("r%s-%s@x.nl" % (cid, i))
            self.conn.execute(
                "INSERT INTO send (campaign_id, subscriber_id, to_email, token,"
                " sent, sent_at) VALUES (?,?,?,?,1,?)",
                (cid, s["id"], s["email"], db.token(), db.now()))
        self.conn.commit()
        return cid

    def test_the_campaigns_page_says_which_list_each_went_to(self):
        self.campaign_to("buyers", name="Kopersmail")
        page = self.client.get("/").get_data(as_text=True)
        self.assertIn("Sent to", page)
        self.assertIn("Bought from us at some point", page)

    def test_the_lists_page_says_what_each_list_has_received(self):
        self.campaign_to("buyers", name="Kopersmail", sent=2)
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertIn("Already sent to it", page)
        self.assertIn("Kopersmail", page)
        self.assertIn("2 sent", page)

    def test_a_list_that_has_had_nothing_says_so(self):
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertIn("Nothing yet", page)

    def test_a_draft_aimed_at_a_list_does_not_count_as_sent_to_it(self):
        """The column answers "what has this already received". A draft has
        been received by nobody, and saying otherwise is the kind of wrong that
        makes somebody send the same thing twice."""
        self.conn.execute(
            "INSERT INTO campaign (name, subject, body, audience, status, created)"
            " VALUES ('Concept','S','<p>x</p>','buyers',?,?)",
            (db.DRAFT, db.now()))
        self.conn.commit()
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertNotIn("Concept", page)
        buyers = page.index("Bought from us at some point")
        self.assertIn("Nothing yet", page[buyers:buyers + 900])

    def test_the_history_is_per_list_not_shared(self):
        """Sending to one list must not look like sending to another."""
        self.campaign_to("buyers", name="Alleen kopers", sent=1)
        page = self.client.get("/lists").get_data(as_text=True)
        buyers = page.index("Bought from us at some point")
        never = page.index("Opted in, never bought anything")
        self.assertIn("Alleen kopers", page[buyers:never])
        self.assertNotIn("Alleen kopers", page[never:])

    def test_a_list_can_be_taken_straight_into_a_campaign(self):
        page = self.client.get("/lists").get_data(as_text=True)
        self.assertIn("Send to this list", page)
        self.assertIn("/campaign/new?audience=buyers", page)

    def test_arriving_with_a_list_preselects_it(self):
        page = self.client.get(
            "/campaign/new?start=plain&audience=buyers").get_data(as_text=True)
        self.assertIn('<option value="buyers" selected>', page)
        self.assertIn("chosen on the Lists screen", page)

    def test_the_list_survives_choosing_a_layout(self):
        """The layout chooser sits between the two screens, and losing the
        audience there would silently aim the campaign at everyone."""
        page = self.client.get("/campaign/new?audience=buyers").get_data(as_text=True)
        self.assertIn("audience=buyers", page)

    def test_a_made_up_list_in_the_url_is_ignored(self):
        page = self.client.get(
            "/campaign/new?start=plain&audience=verzonnen").get_data(as_text=True)
        self.assertNotIn('value="verzonnen"', page)
        self.assertNotIn("chosen on the Lists screen", page)

    def test_saving_the_campaign_keeps_the_chosen_list(self):
        self.client.post("/campaign/new", data={
            "name": "Herfst", "subject": "S", "layout": "plain",
            "audience": "buyers", "f_tekst": "Hallo"})
        row = self.conn.execute(
            "SELECT audience FROM campaign WHERE name='Herfst'").fetchone()
        self.assertEqual(row["audience"], "buyers")

    def test_you_can_see_who_is_in_one(self):
        page = self.client.get("/subscribers?audience=buyers").get_data(as_text=True)
        self.assertIn("koper@x.nl", page)
        self.assertNotIn("stil@x.nl", page)

    def test_the_screen_says_which_list_you_are_looking_at(self):
        page = self.client.get("/subscribers?audience=buyers").get_data(as_text=True)
        self.assertIn("Bought from us at some point", page)
        self.assertIn("Show everyone instead", page)
        plain = self.client.get("/subscribers").get_data(as_text=True)
        self.assertNotIn("Show everyone instead", plain)

    def test_filtering_inside_a_list_stays_inside_it(self):
        page = self.client.get(
            "/subscribers?audience=buyers&q=stil").get_data(as_text=True)
        self.assertNotIn("stil@x.nl", page)

    def test_a_bulk_action_inside_a_list_stays_inside_it(self):
        """Selecting "all matching" while looking at a list has to mean that
        list, or the action reaches people who were never on screen."""
        self.client.post("/subscribers/bulk", data={
            "action": "unsubscribe", "all_matching": "1", "audience": "buyers"})
        rows = dict(self.conn.execute(
            "SELECT email, consent FROM subscriber").fetchall())
        self.assertEqual(rows["koper@x.nl"], db.NO)
        self.assertEqual(rows["stil@x.nl"], db.YES)

    def test_an_unknown_audience_shows_everyone_rather_than_failing(self):
        r = self.client.get("/subscribers?audience=verzonnen")
        self.assertEqual(r.status_code, 200)
        self.assertIn("koper@x.nl", r.get_data(as_text=True))

    def test_paging_and_sorting_keep_the_list_you_are_in(self):
        page = self.client.get(
            "/subscribers?audience=buyers&per=50").get_data(as_text=True)
        self.assertIn("audience=buyers", page)


class SavedListTests(Base):
    """Saving the filter on screen as a named list you can send to."""

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        for email, source, spent in (("koper@x.nl", "customer", 1500.0),
                                     ("stil@x.nl", "crm", 0.0),
                                     ("shop@x.nl", "shopify", 200.0)):
            s = self.sub(email)
            self.conn.execute(
                "UPDATE subscriber SET source = ?, spent = ?, orders = ?"
                " WHERE id = ?", (source, spent, 1 if spent else 0, s["id"]))
        self.conn.commit()

    def members(self, key):
        where, params = sender.audience_sql_for(self.conn, key)
        return {r["email"] for r in self.conn.execute(
            "SELECT email FROM subscriber WHERE " + where, params)}

    def test_a_filter_can_be_saved_and_then_sent_to(self):
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "spend": "yes"})
        seg = db.segments(self.conn)[0]
        self.assertEqual(seg["name"], "Kopers")
        self.assertEqual(self.members("list:%s" % seg["id"]),
                         {"koper@x.nl", "shop@x.nl"})
        # And it turns up where a campaign is aimed.
        self.assertIn(("list:%s" % seg["id"], "Kopers"),
                      sender.audiences(self.conn))

    def test_a_saved_list_stores_the_filter_not_the_people(self):
        """A frozen set of ids would keep mailing somebody who left."""
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "spend": "yes"})
        seg = db.segments(self.conn)[0]
        gone = self.conn.execute(
            "SELECT id FROM subscriber WHERE email='koper@x.nl'").fetchone()
        db.unsubscribe(self.conn, gone["id"], by=db.BY_SELF)
        self.conn.commit()
        self.assertEqual(self.members("list:%s" % seg["id"]), {"shop@x.nl"})

    def test_a_saved_list_can_never_widen_the_consent_floor(self):
        """Saving a filter of "no consent" must not create a list that mails
        people who never opted in."""
        self.client.post("/subscribers/list/save",
                         data={"name": "Geen toestemming", "state": "never"})
        seg = db.segments(self.conn)[0]
        self.assertEqual(self.members("list:%s" % seg["id"]), set())

    def test_saving_without_a_filter_is_refused(self):
        r = self.client.post("/subscribers/list/save",
                             data={"name": "Iedereen"}, follow_redirects=True)
        self.assertEqual(db.segments(self.conn), [])
        self.assertIn("Narrow it first", r.get_data(as_text=True))

    def test_saving_without_a_name_is_refused(self):
        self.client.post("/subscribers/list/save", data={"spend": "yes"})
        self.assertEqual(db.segments(self.conn), [])

    def test_saving_the_same_name_twice_updates_rather_than_duplicates(self):
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "spend": "yes"})
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "source": "shopify"})
        self.assertEqual(len(db.segments(self.conn)), 1)
        seg = db.segments(self.conn)[0]
        self.assertEqual(self.members("list:%s" % seg["id"]), {"shop@x.nl"})

    def test_deleting_a_list_removes_nobody(self):
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "spend": "yes"})
        seg = db.segments(self.conn)[0]
        before = self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0]
        self.client.post("/subscribers/list/%s/delete" % seg["id"])
        self.assertEqual(db.segments(self.conn), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0],
            before)

    def test_a_list_that_no_longer_exists_falls_back_to_everyone_not_a_crash(self):
        where, params = sender.audience_sql_for(self.conn, "list:9999")
        rows = self.conn.execute(
            "SELECT COUNT(*) FROM subscriber WHERE " + where, params).fetchone()[0]
        self.assertEqual(rows, 3)
        for junk in ("list:", "list:abc", "list:1;DROP TABLE subscriber"):
            sender.audience_sql_for(self.conn, junk)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM subscriber").fetchone()[0], 3)

    def test_the_save_control_only_appears_when_something_is_filtering(self):
        plain = self.client.get("/subscribers").get_data(as_text=True)
        self.assertNotIn("Save as list", plain)
        filtered = self.client.get("/subscribers?spend=yes").get_data(as_text=True)
        self.assertIn("Save as list", filtered)

    def test_saved_lists_are_shown_on_the_lists_screen_and_nowhere_else(self):
        """They had their own card on Subscribers as well, which is the same
        information in two places and a per-list COUNT on every page load."""
        self.client.post("/subscribers/list/save",
                         data={"name": "Kopers", "spend": "yes"})
        subs = self.client.get("/subscribers").get_data(as_text=True)
        self.assertNotIn("Your saved lists", subs)
        # Not the name itself: it legitimately appears in the confirmation
        # message right after saving. The card is what had to go.
        self.assertNotIn("See who is in it", subs)
        lists = self.client.get("/lists").get_data(as_text=True)
        self.assertIn("Kopers", lists)


class AudienceTests(Base):
    """What each audience actually means.

    Both of the earlier ones silently became wrong when the CRM import ran:
    "never bought" reported 3.607 people when the answer was 326, and "recent"
    meant everybody because it measured when the row was created. An audience
    that quietly means "everyone" is the most dangerous object in this tool."""

    def person(self, email, spent=0.0, last=""):
        s = self.sub(email)
        self.conn.execute(
            "UPDATE subscriber SET spent = ?, orders = ?, last_order_at = ?"
            " WHERE id = ?", (spent, 1 if spent else 0, last, s["id"]))
        self.conn.commit()
        return s

    def setUp(self):
        super().setUp()
        import datetime
        today = datetime.date.today()
        self.person("nu@x.nl", 1500.0, str(today - datetime.timedelta(days=30)))
        self.person("vorigjaar@x.nl", 900.0, str(today - datetime.timedelta(days=400)))
        self.person("lang@x.nl", 2500.0, str(today - datetime.timedelta(days=1500)))
        self.person("nooit@x.nl")

    def names(self, audience):
        where, params = sender.audience_sql(audience)
        return {r["email"] for r in self.conn.execute(
            "SELECT email FROM subscriber WHERE " + where, params)}

    def test_never_bought_means_never_bought(self):
        self.assertEqual(self.names("never_bought"), {"nooit@x.nl"})

    def test_buyers_means_everybody_who_spent_something(self):
        self.assertEqual(self.names("buyers"),
                         {"nu@x.nl", "vorigjaar@x.nl", "lang@x.nl"})

    def test_the_recency_windows_are_real_windows(self):
        self.assertEqual(self.names("buyers_12m"), {"nu@x.nl"})
        self.assertEqual(self.names("buyers_24m"), {"nu@x.nl", "vorigjaar@x.nl"})

    def test_lapsed_is_only_people_who_bought_long_ago(self):
        self.assertEqual(self.names("lapsed"), {"lang@x.nl"})

    def test_no_audience_is_secretly_everyone(self):
        everyone = self.names("all")
        for key, _label in sender.AUDIENCES:
            if key == "all":
                continue
            self.assertNotEqual(self.names(key), everyone, key)

    def test_every_audience_still_sits_on_the_consent_floor(self):
        out = self.person("weg@x.nl", 5000.0, "2026-01-01")
        db.unsubscribe(self.conn, out["id"], by=db.BY_SELF)
        self.conn.commit()
        for key, _label in sender.AUDIENCES:
            self.assertNotIn("weg@x.nl", self.names(key), key)

    def test_queueing_takes_the_newest_buyers_first(self):
        camp = self.campaign()
        sender.queue(self.conn, camp["id"], "buyers", limit=1)
        queued = [r["to_email"] for r in self.conn.execute(
            "SELECT to_email FROM send WHERE campaign_id = ?", (camp["id"],))]
        self.assertEqual(queued, ["nu@x.nl"])

    def test_pressing_it_again_takes_the_NEXT_batch(self):
        """The whole point of a warm-up: 250 today, the next 250 tomorrow."""
        camp = self.campaign()
        self.assertEqual(sender.queue(self.conn, camp["id"], "buyers", limit=1), 1)
        self.assertEqual(sender.queue(self.conn, camp["id"], "buyers", limit=1), 1)
        queued = [r["to_email"] for r in self.conn.execute(
            "SELECT to_email FROM send WHERE campaign_id = ? ORDER BY id",
            (camp["id"],))]
        self.assertEqual(queued, ["nu@x.nl", "vorigjaar@x.nl"])

    def test_it_stops_when_the_audience_runs_out(self):
        camp = self.campaign()
        self.assertEqual(sender.queue(self.conn, camp["id"], "buyers", limit=100), 3)
        self.assertEqual(sender.queue(self.conn, camp["id"], "buyers", limit=100), 0)

    def test_no_limit_still_queues_everybody(self):
        camp = self.campaign()
        self.assertEqual(sender.queue(self.conn, camp["id"], "all"), 4)


class FlowTests(Base):
    """The abandoned checkout reminders.

    Guard 2 of this tool was "nothing sends on a trigger or a timer". A flow
    breaks that by definition, so almost everything here is about what it
    refuses to do rather than what it does."""

    def setUp(self):
        super().setUp()
        for t in ("flow_send", "flow", "cart_event"):
            try:
                self.conn.execute("DELETE FROM " + t)
            except Exception:
                pass
        self.conn.commit()
        db.seed_flows(self.conn)
        self.flow = flows.find(self.conn, "cart")
        config.FLOWS_ENABLED = True
        config.FLOWS_FROM = "2026-01-01"
        config.FLOW_DAILY_CAP = 100
        flows.NEEDS_CONSENT = False

    def abandoned(self, hours_ago=3, email="klant@x.nl", total=995.0):
        when = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=hours_ago))
        self.conn.execute(
            "INSERT INTO flow (checkout_id, email, name, recovery_url, total,"
            " items, abandoned_at, created) VALUES (?,?,?,?,?,?,?,?)",
            ("co-%s" % email, email, "Sam", "https://example.com/cart/x",
             total, '[{"title":"Bank Example","variant":"","qty":1,"price":"995"}]',
             when.replace(tzinfo=None).isoformat(timespec="seconds"), db.now()))
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM flow WHERE email = ?", (email,)).fetchone()

    def no_orders(self):
        return mock.patch.object(flows, "_has_ordered", return_value=False)

    def fake_code(self):
        """A discount code without asking Shopify for one. The last checkout
        email cannot go out without a code, so a test that forgets this sends
        two emails instead of three and looks like a scheduling bug."""
        return mock.patch.object(flows.discounts, "create",
                                 return_value="ELA10-TEST01")

    def subjects_of(self, key="cart"):
        d = flows.find(self.conn, key)
        return [r["subject"] for r in flows.steps_of(self.conn, d["id"])]

    def later(self, hours):
        """A moment in the future, for walking a sequence forward.

        The 24 hour "nothing else from us today" rule is measured against the
        real clock, so the fixture has to move the flow's own clock rather than
        the machine's: each step is due once enough time has passed since the
        previous one."""
        return (datetime.datetime.now(datetime.timezone.utc)
                + datetime.timedelta(hours=hours))

    # --- the brakes ---------------------------------------------------------

    def test_nothing_sends_while_flows_are_switched_off(self):
        self.abandoned()
        config.FLOWS_ENABLED = False
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertIn("switched off", out["blocked"])
        self.assertEqual(self.sent, [])

    def test_nothing_sends_without_a_start_date(self):
        """Without a floor, switching this on reaches back over every basket
        ever abandoned."""
        self.abandoned()
        config.FLOWS_FROM = ""
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertIn("start date", out["blocked"])
        self.assertEqual(self.sent, [])

    def test_the_main_sending_switch_still_applies_on_top(self):
        self.abandoned()
        config.SENDING_ENABLED = False
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertTrue(out["blocked"])
        self.assertEqual(self.sent, [])

    def test_a_checkout_older_than_the_floor_is_never_even_recorded(self):
        payload = {"checkouts": [{
            "id": 1, "email": "oud@x.nl", "created_at": "2025-06-01T10:00:00Z",
            "total_price": "500", "abandoned_checkout_url": "https://x/y",
            "line_items": [], "customer": {}}]}
        with mock.patch.object(flows, "_shopify", return_value=payload):
            out = flows.poll(self.conn)
        self.assertEqual(out["too_old"], 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM flow").fetchone()[0], 0)

    # --- timing --------------------------------------------------------------

    def test_the_first_reminder_waits_two_hours(self):
        self.abandoned(hours_ago=1)
        self.assertEqual(flows.due(self.conn), [])
        self.abandoned(hours_ago=3, email="later@x.nl")
        self.assertEqual([r["email"] for r in flows.due(self.conn)], ["later@x.nl"])

    def test_the_three_steps_are_two_twenty_four_and_seventy_two_hours(self):
        self.assertEqual(config.FLOW_STEPS, [2, 24, 72])
        row = self.abandoned(hours_ago=30)
        self.conn.execute("UPDATE flow SET step = 1 WHERE id = ?", (row["id"],))
        self.conn.commit()
        self.assertEqual(len(flows.due(self.conn)), 1)     # 30h >= 24h
        self.conn.execute("UPDATE flow SET step = 2 WHERE id = ?", (row["id"],))
        self.conn.commit()
        self.assertEqual(flows.due(self.conn), [])          # 30h < 72h

    # --- what it must never do ----------------------------------------------

    def test_nobody_gets_the_same_step_twice(self):
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
            flows.run(self.conn)
        self.assertEqual(len(self.sent), 1)

    def test_it_stops_the_moment_they_actually_order(self):
        """Nagging somebody who has just given you money is the worst thing
        this feature could do."""
        self.abandoned()
        with mock.patch.object(flows, "_has_ordered", return_value=True), \
             self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(self.sent, [])
        self.assertEqual(out["stopped"], 1)
        row = self.conn.execute("SELECT stopped FROM flow").fetchone()
        self.assertEqual(row["stopped"], flows.STOP_ORDERED)

    def test_a_shopify_outage_is_not_an_answer_and_never_a_purchase(self):
        """It used to return True, meaning "they bought". The caller then wrote
        stopped='ordered' permanently and the screen counted it as a recovered
        sale, so a five second network blip cancelled somebody's sequence and
        invented revenue. Cannot-tell now raises, and the caller waits."""
        with mock.patch.object(flows, "_shopify", side_effect=OSError("down")):
            with self.assertRaises(flows.ShopUnreachable):
                flows._has_ordered("a@x.nl", "2026-01-01")

    def test_an_outage_holds_the_email_back_without_killing_the_flow(self):
        self.abandoned(hours_ago=5)
        with mock.patch.object(flows, "_shopify", side_effect=OSError("down")),                 self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(out["stopped"], 0)            # nobody was written off
        self.assertEqual(out.get("unreachable"), 1)
        row = self.conn.execute("SELECT stopped, step FROM flow").fetchone()
        self.assertEqual(row["stopped"], "")           # still in the sequence
        self.assertEqual(row["step"], 0)               # and has not moved on

    def test_and_it_goes_out_normally_once_the_shop_answers_again(self):
        self.abandoned(hours_ago=5)
        with mock.patch.object(flows, "_shopify", side_effect=OSError("down")),                 self.fake_smtp():
            flows.run(self.conn)
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["sent"], 1)

    def test_nothing_older_than_the_ceiling_ever_enters_a_flow(self):
        """FLOWS_FROM is a date somebody typed once and it shows on no screen.
        Left stale, the first run after switching the flows on would email
        everybody who abandoned a basket in the meantime, all at once."""
        config.FLOWS_FROM = "2020-01-01"
        old_basket = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(hours=flows.MAX_AGE_HOURS + 5))
        payload = {"checkouts": [{
            "id": 4242, "email": "oud@x.nl",
            "created_at": old_basket.isoformat(timespec="seconds"),
            "total_price": "995", "line_items": [],
            "abandoned_checkout_url": "https://example.com/cart/x"}]}
        with mock.patch.object(flows, "_shopify", return_value=payload),                 mock.patch.object(flows, "_has_ordered", return_value=False):
            out = flows.poll(self.conn)
        self.assertEqual(out["added"], 0)
        self.assertEqual(out["too_old"], 1)

    def test_it_never_reaches_somebody_who_unsubscribed(self):
        s = self.sub("klant@x.nl")
        db.unsubscribe(self.conn, s["id"], by=db.BY_SELF)
        self.conn.commit()
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(self.sent, [])
        self.assertEqual(out["stopped"], 1)

    def test_it_never_reaches_a_bounced_address(self):
        s = self.sub("klant@x.nl")
        db.mark_bounced(self.conn, s["id"], "550")
        self.conn.commit()
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        self.assertEqual(self.sent, [])

    def test_the_allowlist_still_applies(self):
        self.abandoned()
        old = config.ALLOWED_RECIPIENTS
        config.ALLOWED_RECIPIENTS = ["iemand.anders@x.nl"]
        try:
            with self.no_orders(), self.fake_smtp():
                flows.run(self.conn)
        finally:
            config.ALLOWED_RECIPIENTS = old
        self.assertEqual(self.sent, [])

    def test_the_daily_cap_bounds_a_runaway(self):
        for i in range(5):
            self.abandoned(email="k%s@x.nl" % i)
        config.FLOW_DAILY_CAP = 2
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["sent"], 2)
        self.assertTrue(out["capped"])

    def test_somebody_with_no_newsletter_consent_still_gets_it(self):
        """They typed their address into your checkout. That is the whole
        basis, and it is what Klaviyo does today."""
        self.sub("klant@x.nl", consent=db.NEVER)
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        self.assertEqual(len(self.sent), 1)

    def test_but_that_can_be_tightened_to_subscribers_only(self):
        self.sub("klant@x.nl", consent=db.NEVER)
        self.abandoned()
        flows.NEEDS_CONSENT = True
        try:
            with self.no_orders(), self.fake_smtp():
                flows.run(self.conn)
        finally:
            flows.NEEDS_CONSENT = False
        self.assertEqual(self.sent, [])

    # --- what it sends -------------------------------------------------------

    def test_the_unsubscribe_link_in_a_reminder_actually_works(self):
        """It 404'd for every flow email. The link was in the message and the
        test only checked it was there, never that it did anything."""
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        token = self.conn.execute(
            "SELECT token FROM flow_send").fetchone()["token"]
        client = webapp.app.test_client()
        webapp.app.config["TESTING"] = True
        r = client.post("/u/%s" % token)
        self.assertEqual(r.status_code, 200)
        row = self.conn.execute(
            "SELECT consent FROM subscriber WHERE email='klant@x.nl'").fetchone()
        self.assertEqual(row["consent"], db.NO)

    def test_unsubscribing_from_a_reminder_stops_every_sequence_they_are_in(self):
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        token = self.conn.execute("SELECT token FROM flow_send").fetchone()["token"]
        webapp.app.config["TESTING"] = True
        webapp.app.test_client().post("/u/%s" % token)
        row = self.conn.execute("SELECT stopped FROM flow").fetchone()
        self.assertEqual(row["stopped"], "unsubscribed")

    def test_opens_and_clicks_on_a_reminder_are_recorded(self):
        """Without this every reminder looked unread forever, because the pixel
        and the click tracker only knew about campaign messages."""
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        token = self.conn.execute("SELECT token FROM flow_send").fetchone()["token"]
        sender.record_open(self.conn, token)
        sender.record_click(self.conn, token)
        row = self.conn.execute(
            "SELECT opened, clicked FROM flow_send WHERE token = ?", (token,)).fetchone()
        self.assertIsNotNone(row["opened"])
        self.assertIsNotNone(row["clicked"])

    def test_one_person_never_runs_two_sequences_at_once(self):
        """Abandoning twice in a week would otherwise mean two reminders a day
        from us, which is exactly the double-email you asked about."""
        self.abandoned(email="klant@x.nl")
        payload = {"checkouts": [{
            "id": 999, "email": "klant@x.nl",
            "created_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(timespec="seconds"),
            "total_price": "500", "abandoned_checkout_url": "https://x/y",
            "line_items": [], "customer": {}}]}
        with mock.patch.object(flows, "_shopify", return_value=payload):
            out = flows.poll(self.conn)
        self.assertEqual(out.get("already_running"), 1)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM flow").fetchone()[0], 1)

    def test_nothing_goes_out_within_a_day_of_anything_else_we_sent(self):
        """A campaign this morning and a reminder this afternoon is two of our
        emails in one day, and neither sender knows about the other."""
        camp = self.campaign()
        s = self.sub("klant@x.nl")
        self.conn.execute(
            "INSERT INTO send (campaign_id, subscriber_id, to_email, token, sent,"
            " sent_at) VALUES (?,?,?,?,1,?)",
            (camp["id"], s["id"], s["email"], db.token(), db.now()))
        self.conn.commit()
        self.abandoned()
        with self.no_orders(), self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(self.sent, [])
        self.assertEqual(out["skipped"], 1)

    def test_the_three_steps_say_different_things(self):
        self.abandoned(hours_ago=100)
        with self.no_orders(), self.fake_smtp(), self.fake_code():
            for hours in (0, 30, 120):
                flows.run(self.conn, self.later(hours))
        subjects = [m["Subject"] for m in self.sent]
        self.assertEqual(subjects, self.subjects_of())
        self.assertEqual(len(set(subjects)), 3)

    def test_shopifys_timezone_is_kept_not_thrown_away(self):
        """Shopify answers in the shop's timezone with the offset attached.
        Cutting the string to 19 characters dropped it, and everything after
        read the result as UTC, so every reminder ran two hours late in summer."""
        self.assertEqual(flows.utc("2026-09-03T10:31:52+02:00"),
                         "2026-09-03T08:31:52+00:00")
        self.assertEqual(flows.utc("2026-09-03T10:31:52Z"),
                         "2026-09-03T10:31:52+00:00")
        # No offset means ours, which is already UTC.
        self.assertEqual(flows.utc("2026-09-03T10:31:52"),
                         "2026-09-03T10:31:52+00:00")

    def test_a_checkout_two_hours_ago_in_amsterdam_is_due_now(self):
        """The end of the same bug: the first reminder waits two hours, so a
        checkout abandoned two hours ago should go now, not in four."""
        config.FLOWS_FROM = "2020-01-01"
        two_hours_ago = (datetime.datetime.now(datetime.timezone.utc)
                         - datetime.timedelta(hours=2, minutes=1))
        amsterdam = two_hours_ago.astimezone(
            datetime.timezone(datetime.timedelta(hours=2)))
        payload = {"checkouts": [{
            "id": 991, "email": "klant@x.nl",
            "created_at": amsterdam.isoformat(timespec="seconds"),
            "total_price": "995", "line_items": [],
            "abandoned_checkout_url": "https://example.com/cart/x"}]}
        with mock.patch.object(flows, "_shopify", return_value=payload),                 mock.patch.object(flows, "_has_ordered", return_value=False):
            flows.poll(self.conn)
        self.assertEqual(len(flows.due(self.conn)), 1)

    def test_a_step_that_failed_is_tried_again_next_run(self):
        """The row is written BEFORE the mail server is asked. If the send then
        fails, the unique index would refuse every later attempt and that
        person would sit at that step for ever, shown as waiting."""
        self.abandoned(hours_ago=5)
        boom = mock.patch.object(sender, "_smtp",
                                 side_effect=OSError("mail server busy"))
        with self.no_orders(), boom:
            first = flows.run(self.conn)
        self.assertEqual(first["sent"], 0)
        row = self.conn.execute("SELECT sent_at, error FROM flow_send").fetchone()
        self.assertIsNone(row["sent_at"])
        self.assertTrue(row["error"])

        with self.no_orders(), self.fake_smtp():
            second = flows.run(self.conn)
        self.assertEqual(second["sent"], 1)
        self.assertEqual(len(self.sent), 1)          # once, not twice
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM flow_send").fetchone()[0], 1)

    def test_a_step_held_back_by_the_allowlist_is_tried_again(self):
        self.abandoned(hours_ago=5)
        config.ALLOWED_RECIPIENTS = ["iemand.anders@x.nl"]
        try:
            with self.no_orders(), self.fake_smtp():
                flows.run(self.conn)
            self.assertEqual(self.sent, [])
            config.ALLOWED_RECIPIENTS = []
            with self.no_orders(), self.fake_smtp():
                out = flows.run(self.conn)
        finally:
            config.ALLOWED_RECIPIENTS = []
        self.assertEqual(out["sent"], 1)

    def test_a_step_that_really_went_is_never_sent_twice(self):
        """The retry must not become a second delivery."""
        self.abandoned(hours_ago=5)
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
            flows.run(self.conn)
            flows.run(self.conn)
        self.assertEqual(len(self.sent), 1)

    def test_it_finishes_after_the_last_step_and_does_not_loop(self):
        self.abandoned(hours_ago=100)
        with self.no_orders(), self.fake_smtp(), self.fake_code():
            for hours in (0, 30, 120, 200, 300, 400):
                flows.run(self.conn, self.later(hours))
        self.assertEqual(len(self.sent), 3)
        row = self.conn.execute("SELECT stopped FROM flow").fetchone()
        self.assertEqual(row["stopped"], flows.STOP_DONE)


    # --- what the screen shows ----------------------------------------------

    def test_the_screen_breaks_the_sequence_down_step_by_step(self):
        """One total cannot tell you which of the three emails is working."""
        self.abandoned(hours_ago=30)
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        rows = flows.steps(self.conn, self.flow)
        self.assertEqual([r["number"] for r in rows], [1, 2, 3])
        # Waits are BETWEEN emails, which is how Klaviyo states them: two hours
        # after they leave, then 22, then 48. The same 2 / 24 / 72 hours from
        # the abandoned basket as before.
        self.assertEqual([r["hours"] for r in rows], [2, 22, 48])
        self.assertEqual(rows[0]["sent"], 1)        # the first went out
        self.assertEqual(rows[1]["sent"], 0)
        self.assertEqual(rows[1]["waiting"], 1)     # and they wait on the second
        self.assertEqual(rows[0]["waiting"], 0)

    def test_an_open_and_a_click_land_on_the_step_that_earned_them(self):
        self.abandoned(hours_ago=30)
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        tok = self.conn.execute("SELECT token FROM flow_send").fetchone()["token"]
        sender.record_open(self.conn, tok)
        sender.record_click(self.conn, tok)
        rows = flows.steps(self.conn, self.flow)
        self.assertEqual((rows[0]["opened"], rows[0]["clicked"]), (1, 1))
        self.assertEqual((rows[1]["opened"], rows[1]["clicked"]), (0, 0))

    def test_the_screen_says_who_was_left_out_and_why(self):
        self.abandoned(hours_ago=30, email="kocht@x.nl")
        with mock.patch.object(flows, "_has_ordered", return_value=True),                 self.fake_smtp():
            flows.run(self.conn)
        left = flows.skipped(self.conn, self.flow)
        self.assertEqual([r["email"] for r in left], ["kocht@x.nl"])
        self.assertEqual(left[0]["stopped"], flows.STOP_ORDERED)

    def test_finishing_the_sequence_is_not_being_left_out(self):
        """Reaching the end is the normal path, not an exception worth listing."""
        self.abandoned(hours_ago=100)
        with self.no_orders(), self.fake_smtp():
            for _ in range(4):
                flows.run(self.conn)
        self.assertEqual(flows.skipped(self.conn, self.flow), [])

    def test_the_flows_screen_lists_every_flow(self):
        """It used to be one hard-wired page for the one flow that exists,
        which read as though that flow was the whole feature."""
        webapp.app.config["TESTING"] = True
        html = webapp.app.test_client().get("/flows").get_data(as_text=True)
        for f in flows.catalog(self.conn):
            self.assertIn(f["name"], html)
        self.assertIn("/flows/cart", html)          # and it opens

    def test_the_flows_screen_is_cards_not_a_spreadsheet(self):
        """The built flow has to be the thing the eye lands on, and the ones
        that are not built must not look clickable."""
        webapp.app.config["TESTING"] = True
        html = webapp.app.test_client().get("/flows").get_data(as_text=True)
        self.assertIn('class="flowcard"', html)          # the live one
        self.assertIn("Open the sequence", html)
        self.assertIn("New flow", html)                  # and she can add one

    def test_the_sequence_is_drawn_as_a_sequence(self):
        webapp.app.config["TESTING"] = True
        html = webapp.app.test_client().get("/flows/cart").get_data(as_text=True)
        self.assertEqual(html.count('class="seq-node seq-mail"'), 3)
        self.assertIn("seq-wait", html)                  # the waits between them
        self.assertIn("Wait 2 hours", html)
        # And the reader, with both languages on the same screen.
        self.assertIn('id="mv-frame"', html)
        self.assertIn('data-lang="en"', html)

    def test_every_step_carries_the_link_the_reader_opens(self):
        """The drawer is JavaScript, so the URL it fetches is the thing worth
        testing: a wrong one fails silently and looks like a broken preview."""
        webapp.app.config["TESTING"] = True
        c = webapp.app.test_client()
        html = c.get("/flows/cart").get_data(as_text=True)
        for step in (1, 2, 3):
            url = "/flows/cart/preview/%s" % step
            self.assertIn('data-preview="%s"' % url, html)
            self.assertEqual(c.get(url).status_code, 200)

    def test_a_flow_that_does_not_exist_is_a_404(self):
        webapp.app.config["TESTING"] = True
        c = webapp.app.test_client()
        self.assertEqual(c.get("/flows/verzonnen").status_code, 404)
        self.assertEqual(c.get("/flows/cart/preview/9").status_code, 404)

    def test_the_flow_screen_renders_the_steps(self):
        webapp.app.config["TESTING"] = True
        self.abandoned(hours_ago=30)
        with self.no_orders(), self.fake_smtp():
            flows.run(self.conn)
        html = webapp.app.test_client().get("/flows/cart").get_data(as_text=True)
        self.assertIn("The sequence", html)
        for subject in self.subjects_of():
            self.assertIn(subject, html)
        # No skipped section while nobody has been skipped. The exact heading,
        # because the page also explains in prose where skipped people appear.
        self.assertNotIn("Left out, and why", html)

    def test_a_preview_shows_a_real_basket_when_there_is_one(self):
        webapp.app.config["TESTING"] = True
        self.abandoned()
        html = webapp.app.test_client().get(
            "/flows/cart/preview/1").get_data(as_text=True)
        self.assertIn("Bank Example", html)

    def test_a_preview_sends_nothing_and_counts_nothing(self):
        webapp.app.config["TESTING"] = True
        self.abandoned()
        with self.fake_smtp():
            webapp.app.test_client().get("/flows/cart/preview/1")
        self.assertEqual(self.sent, [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM flow_send").fetchone()[0], 0)


class FlowBuildingTests(Base):
    """Making and changing a flow from the screen, which is the difference
    between a tool she owns and a tool she has to ask me to change."""

    def setUp(self):
        super().setUp()
        for t in ("flow_send", "flow", "flow_step", "flow_def", "cart_event"):
            try:
                self.conn.execute("DELETE FROM " + t)
            except Exception:
                pass
        self.conn.commit()
        db.seed_flows(self.conn)
        # Without these the runner refuses to do anything at all, and a test
        # that only checks "no email was sent" would pass for the wrong reason.
        config.FLOWS_ENABLED = True
        config.FLOWS_FROM = "2020-01-01"
        config.FLOW_DAILY_CAP = 100
        flows.NEEDS_CONSENT = False
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_a_new_flow_starts_as_a_draft_with_one_email(self):
        """A sequence with no words in it must not be one restart away from
        sending."""
        key = flows.create(self.conn, "Win back", "winback", days=400)
        d = flows.find(self.conn, key)
        self.assertEqual(d["status"], flows.DRAFT)
        self.assertEqual(d["days"], 400)
        self.assertEqual(len(flows.steps_of(self.conn, d["id"])), 1)

    def test_two_flows_with_the_same_name_get_different_addresses(self):
        a = flows.create(self.conn, "Win back", "winback")
        b = flows.create(self.conn, "Win back", "winback")
        self.assertNotEqual(a, b)

    def test_an_unknown_trigger_is_refused(self):
        with self.assertRaises(ValueError):
            flows.create(self.conn, "Hopeful", "telepathy")

    def test_a_flow_cannot_go_live_with_an_empty_subject(self):
        key = flows.create(self.conn, "Win back", "winback")
        d = flows.find(self.conn, key)
        ok, why = flows.set_status(self.conn, d["id"], flows.LIVE)
        self.assertFalse(ok)
        self.assertIn("subject", why)
        self.assertEqual(flows.find(self.conn, key)["status"], flows.DRAFT)

    def test_writing_a_subject_lets_it_go_live_and_pause_again(self):
        key = flows.create(self.conn, "Win back", "winback")
        d = flows.find(self.conn, key)
        flows.save_step(self.conn, d["id"], 0, {"subject": "Wij missen u",
                                                "hours": "48"})
        ok, _why = flows.set_status(self.conn, d["id"], flows.LIVE)
        self.assertTrue(ok)
        self.assertEqual(flows.find(self.conn, key)["status"], flows.LIVE)
        flows.set_status(self.conn, d["id"], flows.PAUSED)
        self.assertEqual(flows.find(self.conn, key)["status"], flows.PAUSED)

    def test_a_paused_flow_sends_nothing_and_loses_nobody(self):
        """Pausing stops the clock. It does not throw away the people who are
        partway through, or they would start again from the top."""
        d = flows.find(self.conn, "cart")
        flows.set_status(self.conn, d["id"], flows.PAUSED)
        self.conn.execute(
            "INSERT INTO flow (def_id, checkout_id, email, name, recovery_url,"
            " total, items, abandoned_at, created) VALUES (?,?,?,?,?,?,?,?,?)",
            (d["id"], "co-1", "klant@x.nl", "Sam", "u", 995, "[]",
             (datetime.datetime.now(datetime.timezone.utc)
              - datetime.timedelta(hours=50)).replace(tzinfo=None)
             .isoformat(timespec="seconds"), db.now()))
        self.conn.commit()
        self.assertEqual(flows.due(self.conn), [])
        self.assertEqual(flows.summary(self.conn, d)["waiting"], 1)

    def test_the_wait_and_the_words_can_be_changed(self):
        d = flows.find(self.conn, "cart")
        flows.save_step(self.conn, d["id"], 0, {
            "hours": "6", "subject": "Nieuw onderwerp", "heading": "Kop",
            "subline": "Beste %(naam)s", "body": "Tekst", "button": "Verder"})
        st = flows.steps(self.conn, d)[0]
        self.assertEqual((st["hours"], st["subject"]), (6.0, "Nieuw onderwerp"))
        _subject, html = flows.preview(self.conn, d, 0)
        self.assertIn("Nieuw onderwerp", _subject)
        self.assertIn("Tekst", html)

    def test_a_silly_wait_is_clamped_rather_than_crashing(self):
        d = flows.find(self.conn, "cart")
        flows.save_step(self.conn, d["id"], 0, {"subject": "x", "hours": "later"})
        self.assertEqual(flows.steps(self.conn, d)[0]["hours"], 24.0)
        flows.save_step(self.conn, d["id"], 0, {"subject": "x", "hours": "-5"})
        self.assertEqual(flows.steps(self.conn, d)[0]["hours"], 0.0)

    def test_adding_and_removing_an_email_keeps_the_numbering_straight(self):
        d = flows.find(self.conn, "cart")
        flows.add_step(self.conn, d["id"])
        self.assertEqual([s["pos"] for s in flows.steps_of(self.conn, d["id"])],
                         [0, 1, 2, 3])
        flows.drop_step(self.conn, d["id"], 1)
        self.assertEqual([s["pos"] for s in flows.steps_of(self.conn, d["id"])],
                         [0, 1, 2])

    def test_the_screens_are_reachable_end_to_end(self):
        r = self.client.post("/flows/new", data={"name": "Welkom",
                                                 "trigger": "welcome"})
        self.assertEqual(r.status_code, 302)
        key = r.headers["Location"].rstrip("/").split("/")[-1]
        self.assertEqual(self.client.get("/flows/%s" % key).status_code, 200)
        self.assertEqual(self.client.get("/flows/%s/step/0" % key).status_code, 200)
        self.assertEqual(self.client.get("/flows/%s/sent" % key).status_code, 200)
        r = self.client.post("/flows/%s/step/0" % key,
                             data={"subject": "Welkom bij Ela", "hours": "1"})
        self.assertEqual(r.status_code, 302)
        d = flows.find(self.conn, key)
        self.assertEqual(flows.steps(self.conn, d)[0]["subject"], "Welkom bij Ela")
        self.client.post("/flows/%s/status" % key, data={"status": "live"})
        self.assertEqual(flows.find(self.conn, key)["status"], flows.LIVE)

    def test_the_sent_screen_lists_every_message_and_who_got_it(self):
        d = flows.find(self.conn, "cart")
        self.conn.execute(
            "INSERT INTO flow (def_id, checkout_id, email, name, recovery_url,"
            " total, items, abandoned_at, created)"
            " VALUES (?,'co-9','klant@x.nl','Sam','u',995,'[]',?,?)",
            (d["id"], db.now(), db.now()))
        fid = self.conn.execute("SELECT id FROM flow").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO flow_send (flow_id, step, to_email, sent_at, token)"
            " VALUES (?,0,'klant@x.nl',?,'TOK9')", (fid, db.now()))
        self.conn.commit()
        rows = flows.sent_messages(self.conn, d)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["to_email"], "klant@x.nl")
        self.assertEqual(rows[0]["subject"],
                         flows.steps_of(self.conn, d["id"])[0]["subject"])
        html = self.client.get("/flows/cart/sent").get_data(as_text=True)
        self.assertIn("klant@x.nl", html)

    # --- the triggers that are not Shopify ----------------------------------

    def cart_event(self, email, source="shop", seen=None, title="Bank Example",
                   price="995"):
        self.conn.execute(
            "INSERT INTO cart_event (email, source, token, cart_url,"
            " product_url, total, items, seen) VALUES (?,?,'',?,?,?,?,?)",
            (email, source, "https://example.com/cart",
             "https://example.com/products/netha", float(price),
             json.dumps([{"title": title, "variant": "", "qty": 1,
                          "price": price}]), seen or db.now()))
        self.conn.commit()

    def hours_ago(self, hours):
        """A stamp in the shape the tables use, for a step that is already due."""
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=hours)).replace(tzinfo=None) \
            .isoformat(timespec="seconds")

    def poll(self):
        """Poll with Shopify silent, so only the trigger under test can fire."""
        config.FLOWS_FROM = "2020-01-01"
        with mock.patch.object(flows, "_shopify", return_value={"checkouts": []}), \
                mock.patch.object(flows, "_has_ordered", return_value=False):
            return flows.poll(self.conn)

    def in_flow(self, key):
        d = flows.find(self.conn, key)
        return [r["email"] for r in self.conn.execute(
            "SELECT email FROM flow WHERE def_id = ? AND stopped = ''",
            (d["id"],)).fetchall()]

    # --- add to cart --------------------------------------------------------

    def test_browse_abandonment_starts_at_add_to_cart(self):
        """The owner's rule: it begins when they put something in the basket
        and do not buy, not when they merely look."""
        self.sub("kijker@x.nl")
        self.cart_event("kijker@x.nl", source="shop")
        self.poll()
        self.assertEqual(self.in_flow("browse"), ["kijker@x.nl"])

    def test_a_basket_filled_from_one_of_our_emails_gets_its_own_flow(self):
        self.sub("klikker@x.nl")
        self.cart_event("klikker@x.nl", source="email")
        self.poll()
        self.assertEqual(self.in_flow("cart-email"), ["klikker@x.nl"])
        self.assertEqual(self.in_flow("browse"), [])

    def test_the_two_add_to_cart_flows_can_never_both_take_one_person(self):
        """Not a rule anybody has to remember: it is which query finds them."""
        self.sub("beide@x.nl")
        self.cart_event("beide@x.nl", source="email")
        self.poll()
        self.assertEqual(self.in_flow("cart-email"), ["beide@x.nl"])
        self.assertEqual(self.in_flow("browse"), [])
        # A second basket, this time not from an email, changes nothing while
        # the first sequence is still running.
        self.cart_event("beide@x.nl", source="shop")
        self.poll()
        self.assertEqual(self.in_flow("browse"), [])

    def test_the_same_basket_does_not_start_the_flow_twice(self):
        self.sub("kijker@x.nl")
        self.cart_event("kijker@x.nl")
        self.poll()
        self.conn.execute("UPDATE flow SET stopped = 'finished'")
        self.conn.commit()
        self.poll()
        d = flows.find(self.conn, "browse")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM flow WHERE def_id = ?", (d["id"],)).fetchone()[0], 1)

    def test_somebody_who_is_not_a_subscriber_is_not_pulled_in(self):
        """Every trigger but the checkout is marketing, and marketing needs a
        subscriber. Refused at the door, so "in it now" stays a number of
        people who will really hear from us."""
        self.cart_event("vreemde@x.nl")
        out = self.poll()
        self.assertEqual(out.get("no_consent"), 1)
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM flow WHERE email = 'vreemde@x.nl'").fetchone())

    # --- buying stops everything -------------------------------------------

    def test_buying_in_the_webshop_stops_the_flow_before_it_starts(self):
        self.sub("koper@x.nl")
        self.cart_event("koper@x.nl")
        config.FLOWS_FROM = "2020-01-01"
        with mock.patch.object(flows, "_shopify", return_value={"checkouts": []}), \
                mock.patch.object(flows, "_has_ordered", return_value=True):
            flows.poll(self.conn)
        self.assertEqual(self.in_flow("browse"), [])
        row = self.conn.execute("SELECT stopped FROM flow WHERE email = 'koper@x.nl'"
                                ).fetchone()
        self.assertEqual(row["stopped"], flows.STOP_ORDERED)

    def test_buying_in_the_showroom_stops_the_flow_too(self):
        """The CRM knows about the showroom and WhatsApp. Shopify does not, and
        this shop takes most of its money that way."""
        self.sub("showroom@x.nl")
        self.cart_event("showroom@x.nl", seen=self.hours_ago(4))
        self.poll()
        self.assertEqual(self.in_flow("browse"), ["showroom@x.nl"])
        self.conn.execute("UPDATE subscriber SET last_order_at = ?"
                          " WHERE email = 'showroom@x.nl'", (db.now(),))
        self.conn.commit()
        with mock.patch.object(flows, "_has_ordered", return_value=False), \
                self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["blocked"], "")     # it really did try to send
        self.assertEqual(self.sent, [])
        self.assertEqual(
            self.conn.execute("SELECT stopped FROM flow WHERE email ="
                              " 'showroom@x.nl'").fetchone()["stopped"],
            flows.STOP_ORDERED)

    def test_an_order_the_results_screen_matched_also_stops_it(self):
        self.sub("gematcht@x.nl")
        self.cart_event("gematcht@x.nl", seen=self.hours_ago(4))
        self.poll()
        self.conn.execute(
            "INSERT INTO attribution (source, order_ref, email, total,"
            " ordered_at, touch, matched_at) VALUES ('showroom','crm-1',?,?,?,"
            "'clicked',?)", ("gematcht@x.nl", 1200.0, db.now(), db.now()))
        self.conn.commit()
        with mock.patch.object(flows, "_has_ordered", return_value=False), \
                self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["blocked"], "")
        self.assertEqual(self.sent, [])
        self.assertEqual(
            self.conn.execute("SELECT stopped FROM flow WHERE email ="
                              " 'gematcht@x.nl'").fetchone()["stopped"],
            flows.STOP_ORDERED)

    # --- one person, one flow ----------------------------------------------

    def test_reaching_the_checkout_takes_them_out_of_the_basket_flow(self):
        """Her rule, exactly: somebody in the abandoned checkout flow is
        skipped by browse abandonment. The checkout is the stronger signal, so
        it wins even when the basket came first."""
        self.sub("beide@x.nl")
        self.cart_event("beide@x.nl")
        self.poll()
        self.assertEqual(self.in_flow("browse"), ["beide@x.nl"])
        checkout = {"checkouts": [{
            "id": 55, "email": "beide@x.nl", "created_at": db.now(),
            "total_price": "1295", "line_items": [],
            "abandoned_checkout_url": "https://example.com/cart/x"}]}
        with mock.patch.object(flows, "_shopify", return_value=checkout), \
                mock.patch.object(flows, "_has_ordered", return_value=False):
            flows.poll(self.conn)
        self.assertEqual(self.in_flow("cart"), ["beide@x.nl"])
        self.assertEqual(self.in_flow("browse"), [])
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM flow WHERE email = 'beide@x.nl'"
                              " AND stopped = ''").fetchone()[0], 1)

    def test_a_basket_never_interrupts_a_checkout_reminder(self):
        """And never the other way round."""
        self.sub("bezig@x.nl")
        checkout = {"checkouts": [{
            "id": 56, "email": "bezig@x.nl", "created_at": db.now(),
            "total_price": "900", "line_items": [],
            "abandoned_checkout_url": "https://example.com/cart/y"}]}
        with mock.patch.object(flows, "_shopify", return_value=checkout), \
                mock.patch.object(flows, "_has_ordered", return_value=False):
            flows.poll(self.conn)
        self.assertEqual(self.in_flow("cart"), ["bezig@x.nl"])
        self.cart_event("bezig@x.nl", source="email")
        self.poll()
        self.assertEqual(self.in_flow("cart"), ["bezig@x.nl"])
        self.assertEqual(self.in_flow("cart-email"), [])

    def test_nobody_is_ever_in_two_flows_at_once(self):
        """Stated once, over every trigger there is."""
        self.sub("alles@x.nl")
        self.conn.execute("UPDATE subscriber SET spent = 5000,"
                          " last_order_at = '2019-01-01' WHERE email = 'alles@x.nl'")
        self.conn.commit()
        self.cart_event("alles@x.nl", source="email")
        self.cart_event("alles@x.nl", source="shop",
                        seen=db.now())
        self.poll()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM flow WHERE email = 'alles@x.nl'"
            " AND stopped = ''").fetchone()[0], 1)

    def test_the_win_back_trigger_uses_what_the_crm_knows(self):
        key = flows.create(self.conn, "Win back", "winback", days=365)
        d = flows.find(self.conn, key)
        flows.save_step(self.conn, d["id"], 0, {"subject": "Wij missen u"})
        flows.set_status(self.conn, d["id"], flows.LIVE)
        # Spend and last order come from the CRM sync, not from a signup.
        for email, spent, last in (("oud@x.nl", 2400, "2020-01-01"),
                                   ("recent@x.nl", 900, db.now()[:10])):
            self.sub(email)
            self.conn.execute(
                "UPDATE subscriber SET spent = ?, last_order_at = ? WHERE email = ?",
                (spent, last, email))
        self.conn.commit()
        config.FLOWS_FROM = "2020-01-01"
        with mock.patch.object(flows, "_shopify", return_value={"checkouts": []}):
            flows.poll(self.conn)
        rows = [r["email"] for r in self.conn.execute(
            "SELECT email FROM flow WHERE def_id = ?", (d["id"],)).fetchall()]
        self.assertEqual(rows, ["oud@x.nl"])

    def test_a_draft_flow_pulls_nobody_in(self):
        key = flows.create(self.conn, "Welkom", "welcome")
        self.sub("nieuw@x.nl")
        config.FLOWS_FROM = "2020-01-01"
        with mock.patch.object(flows, "_shopify", return_value={"checkouts": []}):
            flows.poll(self.conn)
        d = flows.find(self.conn, key)
        self.assertEqual(flows.summary(self.conn, d)["waiting"], 0)

    def test_finishing_a_flow_does_not_put_you_straight_back_into_it(self):
        """The poll runs over and over. Without a key that names the EVENT
        rather than the person, a win back becomes a subscription to itself."""
        key = flows.create(self.conn, "Win back", "winback", days=365)
        d = flows.find(self.conn, key)
        flows.save_step(self.conn, d["id"], 0, {"subject": "Wij missen u"})
        flows.set_status(self.conn, d["id"], flows.LIVE)
        self.sub("oud@x.nl")
        self.conn.execute("UPDATE subscriber SET spent = 2400,"
                          " last_order_at = '2020-01-01' WHERE email = 'oud@x.nl'")
        self.conn.commit()
        config.FLOWS_FROM = "2020-01-01"
        with mock.patch.object(flows, "_shopify", return_value={"checkouts": []}):
            flows.poll(self.conn)
            self.conn.execute("UPDATE flow SET stopped = 'finished'")
            self.conn.commit()
            flows.poll(self.conn)          # a day later, same person
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM flow WHERE def_id = ?",
                              (d["id"],)).fetchone()[0], 1)

    def test_one_person_is_never_in_two_flows_at_once(self):
        """The rule that makes "they got the cart email AND the reminder"
        impossible, now that there can be more than one flow."""
        welcome = flows.find(self.conn, flows.create(self.conn, "Welkom", "welcome"))
        flows.save_step(self.conn, welcome["id"], 0, {"subject": "Welkom"})
        flows.set_status(self.conn, welcome["id"], flows.LIVE)
        self.sub("nieuw@x.nl")
        config.FLOWS_FROM = "2020-01-01"
        checkout = {"checkouts": [{
            "id": 77, "email": "nieuw@x.nl", "created_at": db.now(),
            "total_price": "995", "line_items": [],
            "abandoned_checkout_url": "https://example.com/cart/x"}]}
        with mock.patch.object(flows, "_shopify", return_value=checkout):
            flows.poll(self.conn)
            flows.poll(self.conn)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) FROM flow WHERE email = 'nieuw@x.nl'"
                              " AND stopped = ''").fetchone()[0], 1)


class CartEndpointTests(Base):
    """The one public thing the shop posts to. Everything here is about what it
    refuses, because it is reachable by anybody on the internet."""

    def setUp(self):
        super().setUp()
        for t in ("cart_event", "flow_send", "flow"):
            try:
                self.conn.execute("DELETE FROM " + t)
            except Exception:
                pass
        self.conn.commit()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def post(self, payload):
        return self.client.post("/t/cart", json=payload)

    def events(self):
        return self.conn.execute("SELECT * FROM cart_event ORDER BY id").fetchall()

    def sent_email_to(self, email):
        """One delivered campaign message, and the token that went with it."""
        sub = self.sub(email)
        camp = self.campaign()
        tok = db.token()
        self.conn.execute(
            "INSERT INTO send (campaign_id, subscriber_id, to_email, sent,"
            " sent_at, token) VALUES (?,?,?,1,?,?)",
            (camp["id"], sub["id"], email, db.now(), tok))
        self.conn.commit()
        return tok

    def test_our_own_token_names_the_person_and_the_source(self):
        tok = self.sent_email_to("klant@x.nl")
        self.assertEqual(self.post({"ela": tok, "items": [
            {"title": "Bank Example", "price": "995", "qty": 1}]}).status_code, 204)
        row = self.events()[0]
        self.assertEqual(row["email"], "klant@x.nl")
        self.assertEqual(row["source"], "email")
        self.assertEqual(row["total"], 995.0)

    def test_a_made_up_token_stores_nothing(self):
        self.post({"ela": "verzonnen", "items": []})
        self.assertEqual(self.events(), [])

    def shopify_says(self, answer):
        """What the webshop knows about an address, without asking it."""
        carts._LOOKUPS.clear()
        return mock.patch.object(carts, "_shopify_customer",
                                 return_value=answer)

    def test_an_address_nobody_has_subscribed_is_refused(self):
        """Otherwise the endpoint is a way to start marketing emails to
        strangers."""
        with self.shopify_says(None):
            self.post({"email": "vreemde@x.nl", "items": []})
        self.assertEqual(self.events(), [])

    def test_a_stranger_shopify_also_does_not_know_is_refused(self):
        with self.shopify_says({"consented": False, "buyer": False, "name": ""}):
            self.post({"email": "vreemde@x.nl", "items": []})
        self.assertEqual(self.events(), [])
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM subscriber WHERE email = 'vreemde@x.nl'").fetchone())

    def test_a_signed_in_customer_with_shopify_consent_is_accepted(self):
        """They ticked "email me with news and offers" at checkout. That is
        real opt-in, recorded by Shopify with a timestamp, and it would be
        absurd to refuse the person a reminder they explicitly asked for."""
        with self.shopify_says({"consented": True, "buyer": False,
                                "name": "Sam"}):
            self.post({"email": "nieuw@x.nl", "items": [
                {"title": "Bed", "price": "995", "qty": 1}]})
        self.assertEqual(self.events()[0]["email"], "nieuw@x.nl")
        row = self.conn.execute("SELECT consent, source, name FROM subscriber"
                                " WHERE email = 'nieuw@x.nl'").fetchone()
        self.assertEqual((row["consent"], row["source"]), (db.YES, "shopify"))

    def test_a_signed_in_past_buyer_is_accepted_on_the_customer_basis(self):
        """The same existing-customer basis the owner already invoked for the
        whole CRM import. Provenance says so, rather than pretending opt-in."""
        with self.shopify_says({"consented": False, "buyer": True,
                                "name": "Mehmet"}):
            self.post({"email": "koper@x.nl", "items": []})
        self.assertEqual(self.events()[0]["email"], "koper@x.nl")
        row = self.conn.execute("SELECT consent, source FROM subscriber"
                                " WHERE email = 'koper@x.nl'").fetchone()
        self.assertEqual((row["consent"], row["source"]),
                         (db.YES, "shopify-buyer"))

    def test_an_unsubscribe_is_absolute_whatever_shopify_says(self):
        """No lookup, no second chance. Re-adding somebody who asked to leave
        because another system still lists them is the complaint that gets a
        fresh domain blocked."""
        self.sub("weg@x.nl", consent=db.NO)
        with self.shopify_says({"consented": True, "buyer": True,
                                "name": "X"}) as looked:
            self.post({"email": "weg@x.nl", "items": []})
            looked.assert_not_called()
        self.assertEqual(self.events(), [])
        self.assertEqual(self.conn.execute(
            "SELECT consent FROM subscriber WHERE email = 'weg@x.nl'"
        ).fetchone()["consent"], db.NO)

    def test_shopify_being_unreachable_means_no(self):
        """When the webshop cannot be asked, the safe answer to "may we email
        this person" is never yes."""
        with self.shopify_says(None):
            self.post({"email": "onbekend@x.nl", "items": []})
        self.assertEqual(self.events(), [])

    def test_an_unsubscribed_address_is_refused(self):
        self.sub("weg@x.nl", consent=db.NO)
        with self.shopify_says(None):
            self.post({"email": "weg@x.nl", "items": []})
        self.assertEqual(self.events(), [])

    def test_a_signed_in_customer_who_did_subscribe_is_accepted(self):
        self.sub("klant@x.nl")
        self.post({"email": "KLANT@x.nl", "items": [
            {"title": "Bed", "price": "1.295", "qty": 2}]})
        row = self.events()[0]
        self.assertEqual(row["email"], "klant@x.nl")
        self.assertEqual(row["source"], "shop")
        self.assertEqual(row["total"], 2590.0)

    def test_it_says_the_same_thing_to_everybody(self):
        """A public endpoint that answers differently is a way to ask whether
        an address is on the list."""
        self.sub("klant@x.nl")
        good = self.post({"email": "klant@x.nl", "items": []})
        bad = self.post({"email": "vreemde@x.nl", "items": []})
        junk = self.client.post("/t/cart", data="not json at all")
        for r in (good, bad, junk):
            self.assertEqual(r.status_code, 204)
            self.assertEqual(r.get_data(), b"")

    def test_the_same_person_cannot_fill_the_table(self):
        self.sub("klant@x.nl")
        for _ in range(5):
            self.post({"email": "klant@x.nl", "items": []})
        self.assertEqual(len(self.events()), 1)

    def test_a_link_to_somebody_elses_site_is_dropped(self):
        """The basket link goes straight into an email."""
        self.sub("klant@x.nl")
        self.post({"email": "klant@x.nl",
                   "cart_url": "https://example.com.example.com/cart",
                   "product_url": "javascript:alert(1)", "items": []})
        row = self.events()[0]
        self.assertEqual(row["cart_url"], "")
        self.assertEqual(row["product_url"], "")

    def test_a_relative_product_link_is_completed_not_dropped(self):
        """Shopify's pixel sends the product link relative. Found live: the
        first real event arrived with an empty product because of it."""
        self.sub("klant@x.nl")
        with self.shopify_says(None):
            self.post({"email": "klant@x.nl",
                       "product_url": "/products/terra-3-zitsbank",
                       "items": []})
        self.assertEqual(self.events()[0]["product_url"],
                         "https://example.com/products/terra-3-zitsbank")

    def test_a_protocol_relative_link_is_still_refused(self):
        self.sub("klant@x.nl")
        with self.shopify_says(None):
            self.post({"email": "klant@x.nl",
                       "product_url": "//evil.example/x", "items": []})
        self.assertEqual(self.events()[0]["product_url"], "")

    def test_a_link_to_our_own_shop_is_kept(self):
        self.sub("klant@x.nl")
        self.post({"email": "klant@x.nl",
                   "cart_url": "https://example.com/cart", "items": []})
        self.assertEqual(self.events()[0]["cart_url"], "https://example.com/cart")

    def test_a_page_cannot_smuggle_extra_fields_into_the_email(self):
        self.sub("klant@x.nl")
        self.post({"email": "klant@x.nl", "items": [
            {"title": "<script>x</script>", "price": "10", "qty": 9999,
             "html": "<b>nope</b>", "url": "https://evil.example"}]})
        item = json.loads(self.events()[0]["items"])[0]
        self.assertEqual(sorted(item), ["price", "qty", "title", "variant"])
        self.assertEqual(item["qty"], 99)

    def test_the_browser_is_told_which_shop_may_call_it(self):
        r = self.client.open("/t/cart", method="OPTIONS",
                             headers={"Origin": "https://example.com"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"),
                         "https://example.com")
        r = self.client.open("/t/cart", method="OPTIONS",
                             headers={"Origin": "https://evil.example"})
        self.assertIsNone(r.headers.get("Access-Control-Allow-Origin"))

    def test_a_beacon_posted_as_plain_text_still_arrives(self):
        """sendBeacon posts text/plain, which is what keeps the browser from
        asking permission first."""
        self.sub("klant@x.nl")
        self.client.post("/t/cart", data=json.dumps({"email": "klant@x.nl",
                                                     "items": []}),
                         content_type="text/plain")
        self.assertEqual(len(self.events()), 1)

    def test_the_token_travels_on_every_link_to_our_own_shop(self):
        """Which is the whole reason the shop can tell us who filled a basket
        without a cookie from anybody else."""
        camp = self.campaign(
            body='<a href="https://example.com/products/netha">Kijk</a>'
                 '<a href="https://wa.me/31600000000">App</a>')
        html, _text = sender.render(camp, "Sam", "TOK123")
        self.assertIn("ela%3DTOK123", html)
        self.assertEqual(html.count("ela%3DTOK123"), 1)   # not on wa.me


class BounceTests(Base):
    """The mail that comes back. Getting this wrong in either direction is
    expensive: keep sending to dead addresses and Gmail stops trusting the
    domain; treat a full mailbox as a dead one and you delete real customers."""

    def setUp(self):
        super().setUp()
        self.dir = tempfile.mkdtemp()
        for part in ("new", "cur", "tmp"):
            os.makedirs(os.path.join(self.dir, part), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        super().tearDown()

    def sent_to(self, email):
        """One delivered message, and the token that went out with it."""
        sub = self.sub(email)
        camp = self.campaign()
        tok = db.token()
        self.conn.execute(
            "INSERT INTO send (campaign_id, subscriber_id, to_email, sent,"
            " sent_at, token) VALUES (?,?,?,1,?,?)",
            (camp["id"], sub["id"], email, db.now(), tok))
        self.conn.commit()
        return tok

    def arrives(self, body, name="bounce1"):
        path = os.path.join(self.dir, "new", name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def dsn(self, token, status="5.1.1", extra=""):
        return (
            "From: MAILER-DAEMON@gmail.com\n"
            "To: bounce+%s@mail.example.com\n"
            "Subject: Delivery Status Notification (Failure)\n"
            "Content-Type: multipart/report; report-type=delivery-status;"
            ' boundary="b"\n\n'
            "--b\nContent-Type: text/plain\n\n%s\n"
            "--b\nContent-Type: message/delivery-status\n\n"
            "Final-Recipient: rfc822; klant@x.nl\n"
            "Action: failed\nStatus: %s\n\n--b--\n" % (token, extra, status))

    def scan(self, limit=500):
        return bounces.scan(self.conn, maildir=self.dir, limit=limit)

    def state(self, email):
        return self.conn.execute(
            "SELECT bounced, bounce_reason, consent FROM subscriber"
            " WHERE email = ?", (email,)).fetchone()

    # --- the two verdicts ---------------------------------------------------

    def test_a_dead_address_is_taken_off_the_list(self):
        tok = self.sent_to("weg@x.nl")
        self.arrives(self.dsn(tok, "5.1.1", "550 user unknown"))
        out = self.scan()
        self.assertEqual(out["hard"], 1)
        self.assertTrue(self.state("weg@x.nl")["bounced"])

    def test_a_full_mailbox_is_not_a_dead_address(self):
        """A 4.x.x says the far side was busy. It says nothing about the
        person, and unsubscribing them loses a real customer silently."""
        tok = self.sent_to("vol@x.nl")
        self.arrives(self.dsn(tok, "4.2.2", "452 mailbox full"))
        out = self.scan()
        self.assertEqual((out["hard"], out["soft"]), (0, 1))
        self.assertFalse(self.state("vol@x.nl")["bounced"])

    def test_a_bounced_address_is_never_mailed_again(self):
        tok = self.sent_to("weg@x.nl")
        self.arrives(self.dsn(tok))
        self.scan()
        # The queue builder and every send path check `bounced`.
        rows = sender.audience_count(self.conn, "all")
        self.assertEqual(rows, 0)

    def test_it_also_pulls_them_out_of_a_running_flow(self):
        tok = self.sent_to("weg@x.nl")
        db.seed_flows(self.conn)
        d = flows.find(self.conn, "cart")
        self.conn.execute(
            "INSERT INTO flow (def_id, checkout_id, email, name, recovery_url,"
            " total, items, abandoned_at, created)"
            " VALUES (?,'co-b','weg@x.nl','A','u',1,'[]',?,?)",
            (d["id"], db.now(), db.now()))
        self.conn.commit()
        self.arrives(self.dsn(tok))
        self.scan()
        self.assertEqual(self.conn.execute(
            "SELECT stopped FROM flow WHERE email = 'weg@x.nl'").fetchone()[0],
            "bounced")

    # --- what it refuses to do ----------------------------------------------

    def test_something_that_is_not_a_bounce_is_set_aside_not_deleted(self):
        """An out-of-office reply must not unsubscribe anybody and must not be
        thrown away. It moves to review/ rather than staying in new/, because
        new/ is read in name order with a window: a few hundred auto-replies at
        the front would otherwise block bounce processing for ever."""
        tok = self.sent_to("klant@x.nl")
        path = self.arrives(
            "From: klant@x.nl\nTo: bounce+%s@mail.example.com\n"
            "Subject: Automatisch antwoord\n\nIk ben op vakantie.\n" % tok)
        out = self.scan()
        self.assertEqual(out["hard"], 0)
        self.assertFalse(self.state("klant@x.nl")["bounced"])
        self.assertFalse(os.path.exists(path))          # out of the window
        self.assertEqual(bounces.held_for_review(self.dir), 1)   # but still there

    def test_a_bounce_for_a_token_we_do_not_know_changes_nothing(self):
        self.sub("klant@x.nl")
        self.arrives(self.dsn("verzonnen-token"))
        out = self.scan()
        self.assertEqual(out["hard"], 0)
        self.assertFalse(self.state("klant@x.nl")["bounced"])

    def test_prose_only_bounces_still_land_when_they_are_unambiguous(self):
        """Some servers send no machine-readable part at all."""
        tok = self.sent_to("weg@x.nl")
        self.arrives(
            "From: Mail Delivery Subsystem <MAILER-DAEMON@x.nl>\n"
            "To: bounce+%s@mail.example.com\n"
            "Subject: Mail Delivery Failure\n\n"
            "550 5.1.1 <weg@x.nl>: Recipient address rejected: User unknown\n"
            % tok)
        self.assertEqual(self.scan()["hard"], 1)
        self.assertTrue(self.state("weg@x.nl")["bounced"])

    def test_what_it_dealt_with_is_cleared_away(self):
        tok = self.sent_to("weg@x.nl")
        path = self.arrives(self.dsn(tok))
        self.scan()
        self.assertFalse(os.path.exists(path))

    def test_a_bounce_it_cannot_open_is_counted_loudly(self):
        """The fault that made this whole feature a no-op for two weeks: Postfix
        writes each message 0600, the scanner ran as another user, and the
        unreadable file was skipped BEFORE the counter, so the log printed
        'read: 0' whether the mailbox was empty or entirely unreadable."""
        tok = self.sent_to("weg@x.nl")
        path = self.arrives(self.dsn(tok))
        real_open = builtins.open

        def refuse(p, *a, **kw):
            if str(p) == str(path):
                raise PermissionError(13, "Permission denied")
            return real_open(p, *a, **kw)

        with mock.patch("builtins.open", side_effect=refuse):
            out = self.scan()
        self.assertEqual(out["read"], 0)
        self.assertEqual(out["unreadable"], 1)          # the part that was missing
        self.assertIn("Permission denied", out["error"])
        self.assertFalse(self.state("weg@x.nl")["bounced"])

    def test_a_pile_of_auto_replies_cannot_block_the_bounces_behind_them(self):
        """new/ is read in name order with a 500 file window."""
        tok = self.sent_to("weg@x.nl")
        for i in range(6):
            self.arrives("From: a@b.nl\nTo: bounce+x@mail.example.com\n"
                         "Subject: Automatisch antwoord\n\nweg\n",
                         name="000%s-autoreply" % i)
        self.arrives(self.dsn(tok), name="999-real-bounce")
        self.scan(limit=4)          # a window too small to reach the bounce
        self.scan(limit=4)          # second pass: the replies have moved aside
        self.assertTrue(self.state("weg@x.nl")["bounced"])

    def test_a_missing_mailbox_is_reported_not_raised(self):
        out = bounces.scan(self.conn, maildir="/nergens")
        self.assertIn("error", out)

    def test_the_reason_is_kept_where_somebody_would_look(self):
        tok = self.sent_to("weg@x.nl")
        self.arrives(self.dsn(tok, "5.1.1"))
        self.scan()
        self.assertIn("5.1.1", self.state("weg@x.nl")["bounce_reason"])
        self.assertIn("5.1.1", self.conn.execute(
            "SELECT error FROM send WHERE token = ?", (tok,)).fetchone()[0])

    # --- the return address that makes all of it possible -------------------

    def test_every_message_leaves_with_a_return_address_that_names_it(self):
        old = config.BOUNCE_DOMAIN
        config.BOUNCE_DOMAIN = "mail.example.com"
        try:
            self.assertEqual(config.bounce_address("TOK1"),
                             "bounce+TOK1@mail.example.com")
            handed = {}

            class Server:
                def send_message(self, msg, from_addr=None):
                    handed["from"] = from_addr

            sender.deliver(Server(), "message", "TOK1")
            self.assertEqual(handed["from"], "bounce+TOK1@mail.example.com")
        finally:
            config.BOUNCE_DOMAIN = old

    def test_without_a_bounce_domain_it_sends_exactly_as_before(self):
        old = config.BOUNCE_DOMAIN
        config.BOUNCE_DOMAIN = ""
        try:
            calls = []

            class Server:
                def send_message(self, msg, from_addr=None):
                    calls.append(from_addr)

            sender.deliver(Server(), "message", "TOK1")
            self.assertEqual(calls, [None])
        finally:
            config.BOUNCE_DOMAIN = old


@unittest.skipUnless(config.SHOPIFY_STORE_DOMAIN,
    "needs a Shopify store configured; these passed before only because\n"
    "live credentials happened to be sitting in a .env file next door")
class DiscountCodeTests(Base):
    """One code, one person, 48 hours. The email says so, so it has to be true."""

    def setUp(self):
        super().setUp()
        for t in ("flow_send", "flow", "flow_step", "flow_def", "cart_event"):
            try:
                self.conn.execute("DELETE FROM " + t)
            except Exception:
                pass
        self.conn.commit()
        db.seed_flows(self.conn)
        config.FLOWS_ENABLED = True
        config.FLOWS_FROM = "2020-01-01"
        config.FLOW_DAILY_CAP = 100
        flows.NEEDS_CONSENT = False
        self.d = flows.find(self.conn, "cart")

    def waiting_at_last_step(self, email="klant@x.nl"):
        """Somebody who has had the first two emails and is due the third."""
        self.sub(email)
        stamp = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(hours=100)).replace(tzinfo=None) \
            .isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO flow (def_id, checkout_id, email, name, recovery_url,"
            " total, items, abandoned_at, last_step_at, step, created)"
            " VALUES (?,'co-9',?,'Sam','https://example.com/cart/x',995,'[]',"
            "?,?,2,?)", (self.d["id"], email, stamp, stamp, db.now()))
        self.conn.commit()

    def test_the_code_is_made_per_person_and_kept_with_the_message(self):
        self.waiting_at_last_step()
        with mock.patch.object(flows, "_has_ordered", return_value=False), \
                mock.patch.object(flows.discounts, "create",
                                  return_value="ELA10-AB12CD") as made, \
                self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["sent"], 1)
        made.assert_called_once_with("klant@x.nl", percent=0.1)
        html = self.sent[0].get_body(("html",)).get_content()
        self.assertIn("ELA10-AB12CD", html)
        self.assertIn("discount%3DELA10-AB12CD", html)   # the button applies it
        self.assertEqual(self.conn.execute(
            "SELECT code FROM flow_send").fetchone()["code"], "ELA10-AB12CD")

    def test_no_code_means_no_email_at_all(self):
        """An email built around a discount, carrying a code that does not
        work, is worse than one that arrives an hour late."""
        self.waiting_at_last_step()
        with mock.patch.object(flows, "_has_ordered", return_value=False), \
                mock.patch.object(flows.discounts, "create", return_value=None), \
                self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["sent"], 0)
        self.assertEqual(self.sent, [])

    def test_and_it_tries_again_on_the_next_run(self):
        """The queue row has to go, or the unique index would refuse every
        later attempt and this person would never get the email."""
        self.waiting_at_last_step()
        with mock.patch.object(flows, "_has_ordered", return_value=False), \
                mock.patch.object(flows.discounts, "create", return_value=None), \
                self.fake_smtp():
            flows.run(self.conn)
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM flow_send").fetchone()[0], 0)
        with mock.patch.object(flows, "_has_ordered", return_value=False), \
                mock.patch.object(flows.discounts, "create",
                                  return_value="ELA10-LATER1"), \
                self.fake_smtp():
            out = flows.run(self.conn)
        self.assertEqual(out["sent"], 1)

    def test_two_people_never_share_a_code(self):
        codes = {flows.discounts._code() for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for c in codes:
            self.assertTrue(c.startswith("ELA10-"))
            # No I, O, 0 or 1: a code is read off a phone and typed by hand.
            self.assertNotRegex(c[6:], r"[IO01]")

    def test_what_shopify_is_asked_to_make(self):
        """Single use, one per customer, 48 hours, not combinable. If any of
        that drifts the email starts telling the truth about something else."""
        sent = {}

        def fake(query, variables):
            sent.update(variables["basic"])
            return {"data": {"discountCodeBasicCreate": {
                "codeDiscountNode": {"id": "gid://1"}, "userErrors": []}}}

        with mock.patch.object(flows.discounts, "_graphql", side_effect=fake):
            code = flows.discounts.create("klant@x.nl")
        self.assertTrue(code.startswith("ELA10-"))
        self.assertEqual(sent["usageLimit"], 1)
        # Full-price products only. Without this the code stacks 10% on top of
        # a van/nu price, which is exactly the thing the shop's one-discount
        # rule exists to prevent.
        self.assertEqual(sent["customerGets"]["items"]["collections"]["add"],
                         [flows.discounts.COLLECTION])
        self.assertTrue(sent["appliesOncePerCustomer"])
        self.assertEqual(sent["customerGets"]["value"]["percentage"], 0.10)
        self.assertFalse(any(sent["combinesWith"].values()))
        started = datetime.datetime.fromisoformat(sent["startsAt"].replace("Z", "+00:00"))
        ended = datetime.datetime.fromisoformat(sent["endsAt"].replace("Z", "+00:00"))
        self.assertEqual(round((ended - started).total_seconds() / 3600), 48)

    def test_deleting_a_code_never_touches_a_different_one(self):
        """Shopify's code: search is fuzzy. Searching ELA10-PL6TX9 returns
        WELCOME10 too, because it contains "10". I deleted a live
        discount that way once."""
        found = {"data": {"codeDiscountNodes": {"nodes": [
            {"id": "gid://1", "codeDiscount": {
                "codes": {"nodes": [{"code": "WELCOME10"}]}}},
            {"id": "gid://2", "codeDiscount": {
                "codes": {"nodes": [{"code": "ELA10-PL6TX9"}]}}},
        ]}}}
        killed = []

        def fake(query, variables):
            if "codeDiscountNodes" in query:
                return found
            killed.append(variables["id"])
            return {"data": {"discountCodeDelete": {"deletedCodeDiscountId":
                                                    variables["id"]}}}

        with mock.patch.object(flows.discounts, "_graphql", side_effect=fake):
            gone = flows.discounts.delete("ELA10-PL6TX9")
        self.assertEqual(gone, 1)
        self.assertEqual(killed, ["gid://2"])      # and NOT the wishlist code

    def test_shopify_saying_no_is_not_a_code(self):
        bad = {"data": {"discountCodeBasicCreate": {
            "codeDiscountNode": None,
            "userErrors": [{"field": "code", "message": "already exists"}]}}}
        with mock.patch.object(flows.discounts, "_graphql", return_value=bad):
            self.assertIsNone(flows.discounts.create("klant@x.nl"))

    def test_shopify_being_down_is_not_a_code(self):
        with mock.patch.object(flows.discounts, "_graphql",
                               side_effect=OSError("down")):
            self.assertIsNone(flows.discounts.create("klant@x.nl"))


class TrackingTests(Base):
    """Knowing what an email did. Opens and clicks are the easy half; the half
    that matters for this shop is the order that follows three days later in the
    showroom, which no webshop pixel can see."""

    def setUp(self):
        super().setUp()
        for t in ("attribution", "flow_send", "flow", "cart_event"):
            try:
                self.conn.execute("DELETE FROM " + t)
            except Exception:
                pass
        self.conn.commit()

    def ago(self, hours):
        return (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(hours=hours))

    def sent_email(self, email="klant@x.nl", hours_ago=48, opened=False,
                   clicked=False):
        """One delivered campaign message, `hours_ago` hours back."""
        sub = self.sub(email)
        camp = self.campaign()
        stamp = self.ago(hours_ago).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO send (campaign_id, subscriber_id, to_email, sent,"
            " sent_at, opened, clicked, token) VALUES (?,?,?,1,?,?,?,?)",
            (camp["id"], sub["id"], email, stamp,
             stamp if (opened or clicked) else None,
             stamp if clicked else None, db.token()))
        self.conn.commit()
        return camp

    def orders(self, webshop=None, showroom=None):
        return (mock.patch.object(attrib, "_webshop_orders", return_value=webshop),
                mock.patch.object(attrib, "_showroom_orders", return_value=showroom))

    # --- utm tags -----------------------------------------------------------

    def test_links_carry_utm_tags_so_analytics_can_see_the_email(self):
        """Without these, every visitor from an email is filed under Direct and
        the email looks like it did nothing."""
        camp = self.campaign(
            body='<a href="https://example.com/collections/bedden">Kijk</a>')
        html, _text = sender.render(camp, "Sam", "TOK")
        self.assertIn("utm_source%3Demail", html)
        self.assertIn("utm_medium%3Demail", html)

    def test_utm_tags_go_on_our_own_shop_only(self):
        camp = self.campaign(body='<a href="https://wa.me/31612345678">WhatsApp</a>')
        html, _text = sender.render(camp, "Sam", "TOK")
        self.assertNotIn("utm_source", html)

    def test_a_link_that_already_has_utm_tags_is_left_alone(self):
        """A link built for an ad was built that way deliberately."""
        camp = self.campaign(
            body='<a href="https://example.com/x?utm_source=folder">Kijk</a>')
        html, _text = sender.render(camp, "Sam", "TOK")
        self.assertIn("utm_source%3Dfolder", html)
        self.assertNotIn("utm_source%3Demail", html)

    def test_the_campaign_name_travels_with_the_click(self):
        camp = self.campaign()
        self.conn.execute("UPDATE campaign SET name = 'Herfst actie 2026'"
                          " WHERE id = ?", (camp["id"],))
        self.conn.commit()
        camp = self.conn.execute("SELECT * FROM campaign WHERE id = ?",
                                 (camp["id"],)).fetchone()
        html, _text = sender.render(camp, "Sam", "TOK")
        self.assertIn("utm_campaign%3Dherfst-actie-2026", html)

    # --- what they clicked --------------------------------------------------

    def test_which_link_they_pressed_is_remembered(self):
        self.sent_email()
        tok = self.conn.execute("SELECT token FROM send").fetchone()["token"]
        sender.record_click(self.conn, tok, "https://wa.me/31612345678")
        row = self.conn.execute("SELECT clicked_url FROM send").fetchone()
        self.assertIn("wa.me", row["clicked_url"])
        self.assertEqual(attrib.whatsapp(self.conn), 1)

    def test_a_shop_click_is_not_counted_as_a_whatsapp_message(self):
        self.sent_email()
        tok = self.conn.execute("SELECT token FROM send").fetchone()["token"]
        sender.record_click(self.conn, tok, "https://example.com/collections/bedden")
        self.assertEqual(attrib.whatsapp(self.conn), 0)

    # --- matching orders ----------------------------------------------------

    def test_an_order_after_an_email_is_credited_to_it(self):
        camp = self.sent_email(hours_ago=48, clicked=True)
        order = [("klant@x.nl", "#1001", 995.0,
                  self.ago(24).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=order, showroom=[])
        with shop_p, crm_p:
            out = attrib.match(self.conn)
        self.assertEqual(out["webshop"], 1)
        row = self.conn.execute("SELECT * FROM attribution").fetchone()
        self.assertEqual(row["campaign_id"], camp["id"])
        self.assertEqual(row["touch"], "clicked")
        self.assertAlmostEqual(row["hours"], 24.0, delta=0.5)

    def test_a_showroom_sale_counts_too(self):
        """The whole reason this exists. Eleven webshop orders in the shop's
        history against five thousand in the CRM."""
        self.sent_email(hours_ago=72)
        sale = [("klant@x.nl", "crm-88", 1895.0,
                 self.ago(20).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=[], showroom=sale)
        with shop_p, crm_p:
            out = attrib.match(self.conn)
        self.assertEqual(out["showroom"], 1)
        row = self.conn.execute("SELECT * FROM attribution").fetchone()
        self.assertEqual(row["source"], attrib.SHOWROOM)
        self.assertEqual(row["touch"], "sent")     # weak, and it says so

    def test_an_order_before_the_email_is_not_credited_to_it(self):
        self.sent_email(hours_ago=10)
        order = [("klant@x.nl", "#1001", 995.0,
                  self.ago(40).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=order, showroom=[])
        with shop_p, crm_p:
            attrib.match(self.conn)
        self.assertEqual(attrib.summary(self.conn)["orders"], 0)

    def test_an_order_long_after_the_email_is_not_credited_to_it(self):
        """Otherwise a monthly newsletter quietly claims every sale in the shop."""
        self.sent_email(hours_ago=24 * 40)
        order = [("klant@x.nl", "#1001", 995.0,
                  self.ago(1).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=order, showroom=[])
        with shop_p, crm_p:
            attrib.match(self.conn)
        self.assertEqual(attrib.summary(self.conn)["orders"], 0)

    def test_running_it_twice_does_not_double_the_revenue(self):
        self.sent_email(hours_ago=48)
        order = [("klant@x.nl", "#1001", 995.0,
                  self.ago(24).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=order, showroom=[])
        with shop_p, crm_p:
            attrib.match(self.conn)
            second = attrib.match(self.conn)
        self.assertEqual(second["webshop"], 0)
        self.assertEqual(attrib.summary(self.conn)["value"], 995.0)

    def test_a_clicked_email_beats_a_more_recent_unopened_one(self):
        """Best evidence wins, not merely the last thing that landed."""
        first = self.sent_email(hours_ago=60, clicked=True)
        self.sent_email(email="klant@x.nl", hours_ago=20)
        order = [("klant@x.nl", "#1001", 995.0,
                  self.ago(2).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=order, showroom=[])
        with shop_p, crm_p:
            attrib.match(self.conn)
        row = self.conn.execute("SELECT * FROM attribution").fetchone()
        self.assertEqual(row["campaign_id"], first["id"])
        self.assertEqual(row["touch"], "clicked")

    def test_a_system_it_cannot_read_is_said_out_loud(self):
        """A silent zero from the CRM reads as "email sold nothing in the
        showroom", which is a completely different sentence."""
        self.sent_email()
        shop_p, crm_p = self.orders(webshop=[], showroom=None)
        with shop_p, crm_p:
            out = attrib.match(self.conn)
        self.assertIn(attrib.SHOWROOM, out["unreachable"])

    def test_a_flow_reminder_can_earn_the_order_as_well(self):
        self.sub("klant@x.nl")
        stamp = self.ago(30).isoformat(timespec="seconds")
        self.conn.execute(
            "INSERT INTO flow (checkout_id, email, name, recovery_url, total,"
            " items, abandoned_at, created) VALUES ('co-1',?,'Sam','u',995,"
            " '[]',?,?)", ("klant@x.nl", stamp, db.now()))
        fid = self.conn.execute("SELECT id FROM flow").fetchone()["id"]
        self.conn.execute(
            "INSERT INTO flow_send (flow_id, step, to_email, sent_at, token)"
            " VALUES (?,0,?,?,?)", (fid, "klant@x.nl", stamp, db.token()))
        self.conn.commit()
        order = [("klant@x.nl", "crm-9", 2400.0,
                  self.ago(4).isoformat(timespec="seconds"))]
        shop_p, crm_p = self.orders(webshop=[], showroom=order)
        with shop_p, crm_p:
            attrib.match(self.conn)
        row = self.conn.execute("SELECT * FROM attribution").fetchone()
        self.assertEqual(row["flow_id"], fid)
        self.assertEqual(attrib.by_step(self.conn)[0]["orders"], 1)

    def test_the_results_screen_renders(self):
        webapp.app.config["TESTING"] = True
        self.sent_email()
        html = webapp.app.test_client().get("/results").get_data(as_text=True)
        self.assertIn("Orders after an email", html)
        self.assertIn("WhatsApp", html)


class HouseStyleTests(Base):
    """The owner has asked twice for no long dashes, anywhere, in the interface
    or in what customers receive. A rule stated twice is a rule worth enforcing
    with something other than memory."""

    #: em, en, horizontal bar, figure dash, minus sign, and the HTML entities.
    #: Built from code points, not typed literally, or this file would be the
    #: first thing its own scan reported.
    BANNED = tuple(chr(c) for c in (0x2014, 0x2013, 0x2015, 0x2012, 0x2212)) + tuple(
        "&%s;" % n for n in ("mdash", "ndash", "#8212", "#8211"))

    def test_no_long_dashes_anywhere_in_the_source(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        skip = {".venv", "__pycache__", ".git", "deploy"}
        binary = (".png", ".ico", ".sqlite3", ".webp", ".jpg", ".gif")
        offenders = []
        for folder, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in skip]
            for name in files:
                if name.endswith(binary):
                    continue
                path = os.path.join(folder, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        text = fh.read()
                except (UnicodeDecodeError, OSError):
                    continue
                for n, line in enumerate(text.split("\n"), 1):
                    if any(b in line for b in self.BANNED):
                        offenders.append("%s:%s" % (os.path.relpath(path, root), n))
        self.assertEqual(offenders, [], "long dash in: %s" % offenders)

    def test_no_long_dashes_in_a_rendered_email(self):
        for spec in layouts.LAYOUTS:
            html = layouts.render(spec["key"], layouts.defaults(spec["key"]))
            for bad in self.BANNED:
                self.assertNotIn(bad, html, "%s contains %r" % (spec["key"], bad))


class ShopLookupTests(Base):
    """Reading a product off the webshop. Never touches the network: every test
    replaces the fetch, so a shop outage cannot turn this suite red."""

    def setUp(self):
        super().setUp()
        shop._CACHE.clear()

    def test_it_only_follows_links_on_the_shop(self):
        good = [
            "https://example.com/products/netha",
            "https://www.example.com/products/netha",
            "https://example.com/collections/banken/products/netha?variant=9",
        ]
        for u in good:
            self.assertEqual(shop.handle_of(u), "netha", u)

        # A pasted link must not be able to make the server fetch elsewhere.
        bad = [
            "https://evil.example.com/products/netha",
            "http://169.254.169.254/products/x",
            "file:///etc/passwd",
            "https://example.com/collections/all",
            "https://example.com/products/",
            "not a url",
            "",
        ]
        for u in bad:
            self.assertIsNone(shop.handle_of(u), u)

    def test_prices_come_out_in_the_house_format(self):
        self.assertEqual(shop._money("1495.00"), "EUR 1.495")
        self.assertEqual(shop._money("899"), "EUR 899")
        self.assertEqual(shop._money("12500.00"), "EUR 12.500")
        self.assertEqual(shop._money(None), "")
        self.assertEqual(shop._money("kapot"), "")

    def test_a_product_reads_back_as_a_row(self):
        with mock.patch.object(shop, "_fetch",
                               side_effect=lambda h: _fake_product(h)):
            p = shop.lookup("https://example.com/products/netha")
        self.assertEqual(p["title"], "Example Sofa")
        self.assertEqual(p["price"], "EUR 1.495")
        self.assertEqual(p["was"], "")
        self.assertEqual(p["url"], "https://example.com/products/netha")

    def test_a_markdown_shows_the_old_price(self):
        with mock.patch.object(shop, "_fetch", side_effect=lambda h: _fake_product(
                h, price="1195.00", was="1495.00")):
            p = shop.lookup("https://example.com/products/netha")
        self.assertEqual(p["price"], "EUR 1.195")
        self.assertEqual(p["was"], "EUR 1.495")

    def test_the_same_number_twice_is_not_a_markdown(self):
        with mock.patch.object(shop, "_fetch", side_effect=lambda h: _fake_product(
                h, price="1495.00", was="1495.00")):
            p = shop.lookup("https://example.com/products/netha")
        self.assertEqual(p["was"], "")

    def test_a_shop_that_is_down_returns_nothing_rather_than_raising(self):
        """Somebody writing an email must not be stopped by the webshop."""
        with mock.patch.object(shop, "_fetch", side_effect=OSError("timeout")):
            self.assertIsNone(shop.lookup("https://example.com/products/netha"))

    def test_it_is_fetched_once_not_once_per_keystroke(self):
        calls = []

        def counted(h):
            calls.append(h)
            return _fake_product(h)

        with mock.patch.object(shop, "_fetch", side_effect=counted):
            for _ in range(5):
                shop.lookup("https://example.com/products/netha")
        self.assertEqual(len(calls), 1)


class ProductBlockTests(Base):
    """Products in a campaign, end to end."""

    def setUp(self):
        super().setUp()
        shop._CACHE.clear()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def test_only_the_layouts_that_need_products_have_the_boxes(self):
        for key in ("arrivals", "sale"):
            self.assertIn("product1", layouts.defaults(key), key)
        for key in ("showroom", "plain"):
            self.assertNotIn("product1", layouts.defaults(key), key)

    def test_the_rows_carry_photo_name_price_and_link(self):
        products = [{"title": "Example Sofa", "price": "EUR 1.495", "was": "",
                     "image": "https://cdn.shopify.com/x/netha.webp",
                     "url": "https://example.com/products/netha"}]
        html = layouts.render("arrivals", layouts.defaults("arrivals"), products)
        self.assertIn("Example Sofa", html)
        self.assertIn("EUR 1.495", html)
        self.assertIn("cdn.shopify.com/x/netha.webp", html)
        self.assertIn("example.com/products/netha", html)
        self.assertIn("width:72px;height:72px", html)   # the cart-email row

    def test_no_products_means_no_empty_table(self):
        html = layouts.render("arrivals", layouts.defaults("arrivals"), [])
        self.assertNotIn("width:72px", html)

    def test_saving_bakes_the_price_into_the_email(self):
        """The send loop must never depend on the webshop being reachable."""
        with mock.patch.object(shop, "_fetch", side_effect=lambda h: _fake_product(h)):
            self.client.post("/campaign/new", data={
                "name": "Herfst", "subject": "S", "audience": "all",
                "layout": "arrivals", "f_kop": "Nieuw binnen",
                "f_product1": "https://example.com/products/netha"})
        row = self.conn.execute(
            "SELECT * FROM campaign WHERE name='Herfst'").fetchone()
        self.assertIn("EUR 1.495", row["body"])
        self.assertIn("Example Sofa", row["body"])
        # And the link itself is kept, so the box is still filled in on reopen.
        self.assertIn("example.com/products/netha", row["content"])

    def test_a_link_that_cannot_be_read_is_reported_not_silently_dropped(self):
        with mock.patch.object(shop, "_fetch", side_effect=OSError("down")):
            self.client.post("/campaign/new", data={
                "name": "Herfst", "subject": "S", "audience": "all",
                "layout": "arrivals", "f_kop": "Nieuw binnen",
                "f_product1": "https://example.com/products/netha"})
            row = self.conn.execute(
                "SELECT id FROM campaign WHERE name='Herfst'").fetchone()
            page = self.client.get("/campaign/%s" % row["id"]).get_data(as_text=True)
        self.assertIn("Could not read this one", page)

    def test_a_price_change_since_saving_is_pointed_out(self):
        with mock.patch.object(shop, "_fetch", side_effect=lambda h: _fake_product(h)):
            self.client.post("/campaign/new", data={
                "name": "Herfst", "subject": "S", "audience": "all",
                "layout": "arrivals", "f_kop": "Nieuw binnen",
                "f_product1": "https://example.com/products/netha"})
        row = self.conn.execute(
            "SELECT id FROM campaign WHERE name='Herfst'").fetchone()

        # The shop drops the price afterwards. The saved email still says 1.495.
        shop._CACHE.clear()
        with mock.patch.object(shop, "_fetch", side_effect=lambda h: _fake_product(
                h, price="1195.00")):
            page = self.client.get("/campaign/%s" % row["id"]).get_data(as_text=True)
        self.assertIn("has changed since this was last saved", page)

    def test_no_warning_when_nothing_has_moved(self):
        with mock.patch.object(shop, "_fetch", side_effect=lambda h: _fake_product(h)):
            self.client.post("/campaign/new", data={
                "name": "Herfst", "subject": "S", "audience": "all",
                "layout": "arrivals", "f_kop": "Nieuw binnen",
                "f_product1": "https://example.com/products/netha"})
            row = self.conn.execute(
                "SELECT id FROM campaign WHERE name='Herfst'").fetchone()
            page = self.client.get("/campaign/%s" % row["id"]).get_data(as_text=True)
        self.assertNotIn("has changed since this was last saved", page)


class HouseEmailTests(Base):
    """Where the required parts land now that a layout is a whole document."""

    def laid_out(self, **over):
        v = dict(layouts.defaults("arrivals"))
        v.update(over)
        return self.campaign(body=layouts.render("arrivals", v))

    def test_the_footer_lands_inside_the_design_not_underneath_it(self):
        camp = self.laid_out()
        html, _ = sender.render(camp, "Sam", "tok123")
        self.assertNotIn(layouts.FOOTER_MARK, html)      # marker consumed
        self.assertIn("Your Shop BV", html)        # postal address present
        self.assertIn("/u/tok123", html)                 # and the way out
        # Inside the cream footer cell, so before the document closes.
        self.assertLess(html.index("Afmelden"), html.index("</body>"))

    def test_a_campaign_without_the_marker_still_gets_a_footer(self):
        """The guarantee must not depend on a layout remembering to ask."""
        old = self.campaign(body="<p>Met de hand geschreven</p>")
        html, _ = sender.render(old, "Sam", "tok123")
        self.assertIn("Your Shop BV", html)
        self.assertIn("/u/tok123", html)

    def test_the_unsubscribe_link_is_still_not_click_tracked(self):
        camp = self.laid_out()
        html, _ = sender.render(camp, "", "tok123")
        self.assertIn('href="%s/u/tok123"' % config.PUBLIC_URL, html)
        self.assertNotIn("/c/tok123?u=%s/u/" % config.PUBLIC_URL, html)

    def test_the_preheader_goes_where_the_layout_puts_it(self):
        camp = self.conn.execute(
            "INSERT INTO campaign (name, subject, preheader, body, audience, created)"
            " VALUES ('C','S','Alleen deze maand',?,'all',?)",
            (layouts.render("arrivals", layouts.defaults("arrivals")), db.now()))
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM campaign WHERE id = ?",
                                (camp.lastrowid,)).fetchone()
        html, _ = sender.render(row, "", "tok123")
        self.assertNotIn(layouts.PREHEADER_MARK, html)
        self.assertIn("Alleen deze maand", html)
        # After <body>, not shoved in front of the doctype.
        self.assertTrue(html.startswith("<!DOCTYPE"))
        self.assertLess(html.index("<body"), html.index("Alleen deze maand"))


class BlockRegistryTests(Base):
    """The blocks themselves, before any screen is involved."""

    def test_every_block_in_the_palette_is_a_real_block(self):
        # The palette is what the owner is offered. An entry with no renderer
        # would put a block in her template that draws nothing, with no error.
        for key in layouts.BLOCK_ORDER:
            self.assertIn(key, layouts.BLOCKS, key)
        self.assertEqual(sorted(layouts.BLOCK_ORDER), sorted(layouts.BLOCKS))

    def test_every_block_renders_from_nothing(self):
        """An empty box must produce nothing, never a crash and never a gap.

        This is the whole premise of the builder: add a block, see it, fill it
        in. If a block needs its boxes filled before it can be drawn, adding it
        breaks the page instead of showing an empty version of itself.
        """
        for key in layouts.BLOCKS:
            html = layouts.render_blocks([key], {})
            self.assertIn("<", html, key)

    def test_an_unknown_block_is_skipped_rather_than_fatal(self):
        # Blocks could be renamed in a future version while a stored template
        # still names the old one. Losing a section beats losing the screen.
        html = layouts.render_blocks(["hero", "was-verwijderd", "text"],
                                     {"kop": "Kop", "tekst": "Tekst"})
        self.assertIn("Kop", html)
        self.assertIn("Tekst", html)

    def test_a_box_two_blocks_share_is_offered_once(self):
        # Both headers use "kop". Two boxes with the same name in one form
        # silently overwrite each other.
        keys = [f[0] for f in layouts.fields_for_blocks(["hero", "hero_warm"])]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("kop", keys)

    def test_every_built_in_layout_can_be_taken_apart_into_blocks(self):
        # Starting from something is the normal case, so this has to hold for
        # all ten of them, not just the ones that were tried by hand.
        for layout in layouts.LAYOUTS:
            blocks = layouts.blocks_of(layout["key"])
            self.assertTrue(blocks, layout["key"])
            for b in blocks:
                self.assertIn(b, layouts.BLOCKS, layout["key"])

    def test_a_copy_of_a_built_in_layout_says_what_the_original_says(self):
        """The copy has to be the same email, or "start from this one" is a lie.

        Checked on the words rather than byte for byte: the copy is assembled
        from blocks and the original from a purpose-written function, so the
        wrapper markup legitimately differs.
        """
        for key in ("arrivals", "showroom", "promo"):
            values = layouts.defaults(key)
            original = layouts.render(key, values)
            copy_html = layouts.render_blocks(layouts.blocks_of(key), values)
            for field in layouts.get(key)["fields"]:
                text = (values.get(field[0]) or "").strip()
                if len(text) > 12 and text in original:
                    self.assertIn(text, copy_html, "%s / %s" % (key, field[0]))


class TemplateBuilderTests(Base):
    """Making a template, changing it, and using it for a real campaign."""

    def setUp(self):
        super().setUp()
        self.conn.execute("DELETE FROM template")
        self.conn.commit()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def make(self, start="", name="Mijn sjabloon"):
        self.client.post("/templates/new", data={"start": start, "name": name})
        return self.conn.execute(
            "SELECT * FROM template ORDER BY id DESC LIMIT 1").fetchone()

    def blocks(self, tid):
        row = self.conn.execute("SELECT blocks FROM template WHERE id = ?",
                                (tid,)).fetchone()
        return json.loads(row["blocks"])

    # --- making one ---------------------------------------------------------

    def test_starting_from_nothing_gives_a_template_that_already_renders(self):
        t = self.make()
        self.assertTrue(self.blocks(t["id"]))
        html = layouts.render("t:%s" % t["id"], {})
        self.assertIn("<!--FOOTER-->", html)

    def test_starting_from_a_layout_copies_its_blocks_and_its_words(self):
        t = self.make(start="showroom")
        self.assertEqual(self.blocks(t["id"]), layouts.blocks_of("showroom"))
        self.assertEqual(t["subject"], layouts.get("showroom")["subject"])
        content = json.loads(t["content"])
        self.assertEqual(content, layouts.defaults("showroom"))

    def test_a_template_can_be_started_from_another_template(self):
        first = self.make(start="promo", name="Promo basis")
        second = self.make(start="t:%s" % first["id"], name="Promo kerst")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.blocks(second["id"]), self.blocks(first["id"]))

    def test_an_unnamed_template_is_named_after_what_it_came_from(self):
        t = self.make(start="arrivals", name="")
        self.assertIn(layouts.get("arrivals")["name"], t["name"])

    # --- the loader hook ----------------------------------------------------

    def test_layouts_can_find_a_template_that_lives_in_the_database(self):
        t = self.make(start="arrivals")
        spec = layouts.get("t:%s" % t["id"])
        self.assertIsNotNone(spec)
        self.assertEqual(spec["name"], t["name"])
        self.assertTrue(spec["fields"])

    def test_a_template_that_is_gone_is_not_a_layout(self):
        t = self.make()
        self.client.post("/templates/%s/delete" % t["id"])
        self.assertIsNone(layouts.get("t:%s" % t["id"]))

    def test_a_made_up_template_key_is_not_a_layout(self):
        for key in ("t:", "t:abc", "t:999999", "t:-1"):
            self.assertIsNone(layouts.get(key), key)

    def test_stored_rubbish_does_not_take_the_screen_down(self):
        # Hand-edited, half-written by an interrupted deploy, whatever. The
        # screen must open so it can be fixed, not 500 so it cannot.
        t = self.make()
        self.conn.execute("UPDATE template SET blocks = ?, content = ? WHERE id = ?",
                          ("{not json", "{not json", t["id"]))
        self.conn.commit()
        spec = layouts.get("t:%s" % t["id"])
        self.assertEqual(spec["blocks"], [])
        self.assertEqual(self.client.get("/templates/%s" % t["id"]).status_code, 200)

    # --- the screens --------------------------------------------------------

    def test_the_editor_is_shaped_like_the_composer_so_the_preview_works(self):
        t = self.make(start="arrivals")
        page = self.client.get("/templates/%s" % t["id"]).get_data(as_text=True)
        self.assertIn('id="composer"', page)
        self.assertIn('id="preview"', page)
        self.assertIn('name="layout" value="t:%s"' % t["id"], page)
        self.assertIn('data-field="kop"', page)

    def test_the_editor_shows_the_words_that_are_stored(self):
        t = self.make(start="arrivals")
        self.client.post("/templates/%s" % t["id"],
                         data={"name": t["name"], "f_kop": "Herfstcollectie"})
        page = self.client.get("/templates/%s" % t["id"]).get_data(as_text=True)
        self.assertIn("Herfstcollectie", page)

    def test_the_list_shows_her_own_templates(self):
        self.make(name="Showroom weekend")
        page = self.client.get("/templates").get_data(as_text=True)
        self.assertIn("Showroom weekend", page)

    def test_the_preview_renders_a_template_of_her_own(self):
        t = self.make(start="arrivals")
        r = self.client.post("/api/preview", json={
            "layout": "t:%s" % t["id"], "values": {"kop": "Voorbeeldkop"}})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Voorbeeldkop", r.get_data(as_text=True))

    # --- blocks -------------------------------------------------------------

    def test_adding_a_block_puts_it_on_the_bottom(self):
        t = self.make()
        before = self.blocks(t["id"])
        self.client.post("/templates/%s/block/add" % t["id"], data={"block": "quote"})
        self.assertEqual(self.blocks(t["id"]), before + ["quote"])

    def test_a_block_that_does_not_exist_is_refused(self):
        t = self.make()
        r = self.client.post("/templates/%s/block/add" % t["id"],
                             data={"block": "<script>"})
        self.assertEqual(r.status_code, 400)

    def test_blocks_move_up_and_down(self):
        t = self.make()
        first, second = self.blocks(t["id"])[:2]
        self.client.post("/templates/%s/block/1/up" % t["id"])
        self.assertEqual(self.blocks(t["id"])[:2], [second, first])
        self.client.post("/templates/%s/block/0/down" % t["id"])
        self.assertEqual(self.blocks(t["id"])[:2], [first, second])

    def test_a_block_can_be_removed(self):
        t = self.make()
        before = self.blocks(t["id"])
        self.client.post("/templates/%s/block/0/remove" % t["id"])
        self.assertEqual(self.blocks(t["id"]), before[1:])

    def test_moving_past_the_end_changes_nothing(self):
        t = self.make()
        before = self.blocks(t["id"])
        self.assertEqual(
            self.client.post("/templates/%s/block/0/up" % t["id"]).status_code, 400)
        self.assertEqual(self.blocks(t["id"]), before)

    def test_a_position_that_is_not_there_is_not_found(self):
        t = self.make()
        r = self.client.post("/templates/%s/block/99/up" % t["id"])
        self.assertEqual(r.status_code, 404)

    def test_moving_a_block_keeps_what_was_typed(self):
        """Every block button submits the whole form, so twenty minutes of
        writing survives pressing an arrow. This is the bug that would make
        somebody stop using the builder without ever saying why."""
        t = self.make(start="arrivals")
        self.client.post("/templates/%s/block/1/up" % t["id"],
                         data={"name": "Herfst", "subject": "Nieuw onderwerp",
                               "f_kop": "Getypt, niet opgeslagen"})
        after = self.conn.execute("SELECT * FROM template WHERE id = ?",
                                  (t["id"],)).fetchone()
        self.assertEqual(json.loads(after["content"])["kop"], "Getypt, niet opgeslagen")
        self.assertEqual(after["subject"], "Nieuw onderwerp")
        self.assertEqual(after["name"], "Herfst")

    def test_removing_a_block_keeps_its_words_in_case_it_comes_back(self):
        t = self.make(start="arrivals")
        self.client.post("/templates/%s" % t["id"],
                         data={"name": t["name"], "f_tekst": "Lang stuk tekst."})
        pos = self.blocks(t["id"]).index("text")
        self.client.post("/templates/%s/block/%s/remove" % (t["id"], pos))
        self.assertNotIn("text", self.blocks(t["id"]))
        self.client.post("/templates/%s/block/add" % t["id"], data={"block": "text"})
        page = self.client.get("/templates/%s" % t["id"]).get_data(as_text=True)
        self.assertIn("Lang stuk tekst.", page)

    def test_a_block_change_never_sends_anything(self):
        t = self.make(start="promo")
        with self.fake_smtp():
            self.client.post("/templates/%s/block/add" % t["id"], data={"block": "quote"})
            self.client.post("/templates/%s/block/0/down" % t["id"])
            self.client.post("/templates/%s" % t["id"], data={"name": "X"})
        self.assertEqual(len(self.sent), 0)

    # --- using one ----------------------------------------------------------

    def test_a_template_of_her_own_is_offered_when_making_a_campaign(self):
        t = self.make(name="Showroom weekend")
        page = self.client.get("/campaign/new").get_data(as_text=True)
        self.assertIn("Showroom weekend", page)
        self.assertIn("start=t:%s" % t["id"], page)

    def test_a_campaign_can_be_written_with_a_template_of_her_own(self):
        t = self.make(start="arrivals", name="Herfst sjabloon")
        self.client.post("/campaign/new", data={
            "name": "Herfst", "subject": "Onderwerp", "audience": "all",
            "layout": "t:%s" % t["id"], "f_kop": "Nieuw binnen",
            "f_tekst": "Kom kijken."})
        camp = self.conn.execute(
            "SELECT * FROM campaign WHERE name='Herfst'").fetchone()
        self.assertEqual(camp["layout"], "t:%s" % t["id"])
        self.assertIn("Nieuw binnen", camp["body"])
        self.assertIn("<!--FOOTER-->", camp["body"])

    def test_deleting_a_template_leaves_finished_campaigns_alone(self):
        """The HTML was baked in when the campaign was saved. A campaign that
        went out last month must not change because a template was tidied up."""
        t = self.make(start="arrivals")
        self.client.post("/campaign/new", data={
            "name": "Herfst", "subject": "Onderwerp", "audience": "all",
            "layout": "t:%s" % t["id"], "f_kop": "Nieuw binnen"})
        self.client.post("/templates/%s/delete" % t["id"])
        camp = self.conn.execute(
            "SELECT * FROM campaign WHERE name='Herfst'").fetchone()
        self.assertIn("Nieuw binnen", camp["body"])
        self.assertEqual(self.client.get("/campaign/%s" % camp["id"]).status_code, 200)

    def test_the_template_screens_are_behind_the_login(self):
        t = self.make()
        old = config.PASSWORD
        config.PASSWORD = "geheim"
        try:
            shut = webapp.app.test_client()
            for url in ("/templates", "/templates/%s" % t["id"]):
                self.assertEqual(shut.get(url).status_code, 302, url)
            for url in ("/templates/new", "/templates/%s/delete" % t["id"],
                        "/templates/%s/block/add" % t["id"],
                        "/templates/%s/block/0/up" % t["id"]):
                self.assertEqual(shut.post(url).status_code, 302, url)
        finally:
            config.PASSWORD = old


class LinkRewritingTests(Base):
    """What arrives at the shop when somebody presses the button.

    These exist because the discount code in the last checkout-recovery email
    did not work. It was printed in the email, the customer pressed the button,
    and Shopify received a parameter called "amp;discount". Nothing looked
    wrong anywhere: the email was right, the code was real, the link opened.
    """

    def shop_sees(self, href):
        """The query Shopify ends up with, after both hops."""
        from urllib.parse import parse_qs, urlsplit
        outer = parse_qs(urlsplit(html_mod.unescape(href)).query)
        return parse_qs(urlsplit(outer["u"][0]).query)

    def hrefs(self, html, needle):
        return [h for h in re.findall(r'href="([^"]+)"', html) if needle in h]

    def test_the_second_query_parameter_keeps_its_name(self):
        html = sender._rewrite_links(
            '<a href="https://example.com/cart/c/ABC?key=deadbeef&amp;discount=ELA10">x</a>',
            "TOK")
        seen = self.shop_sees(self.hrefs(html, "cart")[0])
        self.assertEqual(seen["discount"], ["ELA10"])
        self.assertNotIn("amp;discount", seen)

    def test_the_discount_in_a_recovery_email_actually_reaches_the_checkout(self):
        # End to end through a real layout, because the fault was in the seam
        # between the layout escaping the href and the rewriter reading it.
        body = layouts.render("cart", {
            "kop": "Uw winkelwagen", "actie_code": "ELA10-7K2QX9",
            "knop_tekst": "Afrekenen",
            "knop_link": "https://example.com/cart/c/ABC?key=deadbeef&discount=ELA10-7K2QX9"})
        html, _text = sender.render(
            {"body": body, "subject": "s", "preheader": "", "name": "cart"},
            "Sam", "TOK")
        seen = self.shop_sees(self.hrefs(html, "cart%2Fc%2FABC")[0])
        self.assertEqual(seen["discount"], ["ELA10-7K2QX9"])
        self.assertEqual(seen["key"], ["deadbeef"])

    def test_an_ampersand_written_by_hand_is_left_alone(self):
        """Only &amp; is undone, not every entity.

        An old campaign written as HTML by hand can legitimately contain
        ?a=1&copy=2, and unescaping everything would turn that parameter into a
        copyright sign.
        """
        html = sender._rewrite_links(
            '<a href="https://example.com/x?a=1&copy=2">x</a>', "TOK")
        seen = self.shop_sees(self.hrefs(html, "example.com%2Fx")[0])
        self.assertIn("copy", seen)
        self.assertEqual(seen["copy"], ["2"])

    def test_the_tracking_link_is_valid_html(self):
        # It sits inside href="...", so its own ampersands have to be escaped
        # or the attribute is not what it looks like.
        html = sender._rewrite_links(
            '<a href="https://example.com/x?a=1&amp;b=2">x</a>', "TOK")
        href = self.hrefs(html, "example.com")[0]
        self.assertIn("&amp;s=", href)
        self.assertNotIn("&s=", href.replace("&amp;s=", ""))

    def test_the_signature_still_matches_after_all_that(self):
        html = sender._rewrite_links(
            '<a href="https://example.com/x?a=1&amp;b=2">x</a>', "TOK")
        from urllib.parse import parse_qs, urlsplit
        q = parse_qs(urlsplit(html_mod.unescape(self.hrefs(html, "example")[0])).query)
        self.assertEqual(sender.click_signature("TOK", q["u"][0]), q["s"][0])

    def test_the_plain_text_part_is_read_not_parsed(self):
        """It said "Bank &amp; Fauteuil". That is what a screen reader reads
        out, and what anybody whose client prefers plain text sees."""
        body = layouts.render("arrivals", {"kop": "Bank & Fauteuil",
                                           "tekst": "Vanaf EUR 995 & hoger."})
        _html, text = sender.render(
            {"body": body, "subject": "s", "preheader": "", "name": "x"},
            "Sam", "TOK")
        self.assertIn("Bank & Fauteuil", text)
        self.assertEqual(re.findall(r"&[a-zA-Z]+;", text), [])


class DiscountPercentTests(Base):
    """A code has to be worth what the email said it was worth.

    The promotion layout promised 20% in three places and the generator made a
    10% code every time, because nothing ever told it otherwise. The customer
    reads 20%, types the code at the checkout, and gets 10%.
    """

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def camp_promising(self, **fields):
        data = {"name": "Actie", "subject": fields.pop("subject", "Onderwerp"),
                "audience": "all", "layout": "promo"}
        data.update({"f_" + k: v for k, v in fields.items()})
        self.client.post("/campaign/new", data=data)
        return self.conn.execute(
            "SELECT * FROM campaign WHERE name='Actie'").fetchone()

    def queue_one(self, camp):
        self.sub("klant@x.nl")
        sender.queue(self.conn, camp["id"], "all")
        self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                          (db.SENDING, camp["id"]))
        self.conn.commit()
        return self.conn.execute(
            "SELECT * FROM send WHERE campaign_id = ?", (camp["id"],)).fetchone()

    # --- reading the promise ------------------------------------------------

    def test_a_discount_is_read_out_of_the_words(self):
        self.assertEqual(sender.discounts.promised("20% korting, eenmalig"), {20})
        self.assertEqual(sender.discounts.promised("-20%"), {20})
        self.assertEqual(sender.discounts.promised("Nog 48 uur: 20% korting"), {20})

    def test_a_percentage_that_is_not_a_discount_is_not_read_as_one(self):
        """100% katoen and 0% rente are both real lines in furniture copy.
        Inferring a discount from the word for cotton would create a code that
        takes the whole price off."""
        self.assertEqual(sender.discounts.promised("100% katoen, 0% rente"), set())
        self.assertEqual(sender.discounts.promised("3x renteloos"), set())

    def test_a_code_carries_its_own_percentage_in_its_name(self):
        self.assertTrue(sender.discounts._code(0.20).startswith("ELA20-"))
        self.assertTrue(sender.discounts._code(0.10).startswith("ELA10-"))

    # --- campaigns ----------------------------------------------------------

    def test_the_code_is_worth_what_the_email_promised(self):
        camp = self.camp_promising(kop="Nog 48 uur: 20% korting op alles",
                                   korting="-20%", actie_code=sender.discounts.AUTO,
                                   actie_tekst="20% korting, eenmalig, 48 uur geldig")
        row = self.queue_one(camp)
        with mock.patch.object(discounts, "create",
                               return_value="ELA20-ABC123") as made:
            with self.fake_smtp() as server:
                sender.send_one(self.conn, row, server)
        made.assert_called_once_with("klant@x.nl", percent=0.2)

    def test_ten_per_cent_is_still_ten_per_cent(self):
        camp = self.camp_promising(kop="10% korting op uw bestelling",
                                   actie_code=sender.discounts.AUTO,
                                   actie_tekst="10% korting, eenmalig")
        row = self.queue_one(camp)
        with mock.patch.object(discounts, "create",
                               return_value="ELA10-ABC123") as made:
            with self.fake_smtp() as server:
                sender.send_one(self.conn, row, server)
        made.assert_called_once_with("klant@x.nl", percent=0.1)

    def test_an_email_that_contradicts_itself_sends_nothing(self):
        """Two different percentages in one email is not something to guess at.
        Whichever one is picked, half the recipients were told the other."""
        camp = self.camp_promising(kop="Nog 48 uur: 20% korting op alles",
                                   actie_code=sender.discounts.AUTO,
                                   actie_tekst="10% korting, eenmalig")
        row = self.queue_one(camp)
        with mock.patch.object(discounts, "create") as made:
            with self.fake_smtp() as server:
                ok, why = sender.send_one(self.conn, row, server)
        self.assertFalse(ok)
        self.assertEqual(why, "discount_unclear")
        made.assert_not_called()
        self.assertEqual(len(self.sent), 0)
        after = self.conn.execute("SELECT error FROM send WHERE id = ?",
                                  (row["id"],)).fetchone()
        self.assertIn("20%", after["error"])
        self.assertIn("10%", after["error"])

    def test_a_stylesheet_is_not_a_promise(self):
        """The rendered email is full of width:100%. Reading the promise out of
        the finished HTML works today only because none of those happen to look
        like a discount, which is a trap for whoever edits the layout next."""
        camp = self.camp_promising(kop="Nieuwe collectie", actie_code=sender.discounts.AUTO)
        self.assertIn("100%", camp["body"])
        self.assertEqual(sender.discounts.promised(sender.promise_text(camp)), set())

    def test_saying_nothing_about_a_percentage_still_gets_the_usual_ten(self):
        camp = self.camp_promising(kop="Uw persoonlijke code",
                                   actie_code=sender.discounts.AUTO)
        row = self.queue_one(camp)
        with mock.patch.object(discounts, "create",
                               return_value="ELA10-ABC123") as made:
            with self.fake_smtp() as server:
                sender.send_one(self.conn, row, server)
        made.assert_called_once_with("klant@x.nl", percent=sender.discounts.PERCENT)

    # --- flows --------------------------------------------------------------

    def test_a_flow_step_asks_for_what_its_own_words_promise(self):
        d = self.conn.execute(
            "SELECT * FROM flow_def WHERE key = 'cart'").fetchone()
        step = self.conn.execute(
            "SELECT * FROM flow_step WHERE def_id = ? AND pos = 2",
            (d["id"],)).fetchone()
        rate, why = sender.discounts.rate_for(flows._promise_text(step))
        self.assertEqual(why, "")
        self.assertEqual(rate, 0.10)
        self.assertIn("10%", flows._promise_text(step))


class GreetingTests(Base):
    """Beste Samira, not Beste Samira Azouagh.

    3.857 of the 4.000 names on this list are two words or more, because an
    order form asks for a full name, and the whole thing was being dropped into
    "Beste {{naam}},". Every example below is taken from the real list.
    """

    def test_the_first_name_is_used_not_the_whole_name(self):
        self.assertEqual(sender.first_name("Samira Azouagh"), "Samira")
        self.assertEqual(sender.first_name("Jan van der Berg"), "Jan")

    def test_a_name_typed_in_one_case_is_repaired(self):
        self.assertEqual(sender.first_name("esen alagoz"), "Esen")
        self.assertEqual(sender.first_name("\u00d6ZLEM YILMAZ"), "\u00d6zlem")

    def test_a_name_that_was_typed_properly_is_left_exactly_alone(self):
        """title() would turn McCarthy into Mccarthy, and somebody's own
        spelling of their own name is not ours to tidy."""
        self.assertEqual(sender.first_name("McCarthy Smith"), "McCarthy")
        self.assertEqual(sender.first_name("G\u00f6rkem \u00d6z\u00fcn"), "G\u00f6rkem")

    def test_the_comma_form_from_an_export_is_understood(self):
        self.assertEqual(sender.first_name("Azouagh, Samira"), "Samira")

    def test_an_initial_is_not_a_first_name(self):
        """One in ten of the list is filed as initials and a surname. "Beste
        H." is worse than not using a name at all."""
        for raw in ("H. Kebap", "T Janssen", "N.S Jafra", "E.M Habaths", "J"):
            self.assertEqual(sender.first_name(raw), "", raw)

    def test_a_business_has_no_first_name(self):
        for raw in ("Middelland Holding B.V.", "Automobielbedrijf ED COSTER",
                    "Schildersbedrijf Jansen", "Your Shop BV"):
            self.assertEqual(sender.first_name(raw), "", raw)

    def test_rubbish_in_the_name_field_is_not_greeted(self):
        for raw in ("info@x.nl", "X 123", "", "   ", None):
            self.assertEqual(sender.first_name(raw), "")

    def test_the_fallback_is_dutch(self):
        """It said "Beste daar", which is "Hi there" translated word for word
        and is not something a Dutch shop writes."""
        body = layouts.render("arrivals", {"subline": "Beste {{naam}}, welkom."})
        html, _text = sender.render(
            {"body": body, "subject": "s", "preheader": "", "name": "x"},
            "T Janssen", "TOK")
        self.assertIn("Beste klant,", html)
        self.assertNotIn("Beste daar", html)
        self.assertNotIn("{{naam}}", html)

    def test_the_greeting_uses_the_first_name_end_to_end(self):
        body = layouts.render("arrivals", {"subline": "Beste {{naam}}, welkom."})
        html, text = sender.render(
            {"body": body, "subject": "s", "preheader": "", "name": "x"},
            "samira azouagh", "TOK")
        self.assertIn("Beste Samira,", html)
        self.assertIn("Beste Samira,", text)
        self.assertNotIn("Azouagh", html)

    def test_the_crm_takes_a_name_it_can_greet_somebody_by(self):
        """max(name) was alphabetical, so one address with several customer
        records was greeted with whichever spelling happened to sort last. On
        the live CRM that is 79 addresses, and some of them were being greeted
        by a different member of the household."""
        lines = [l.split("--")[0] for l in crm.CUSTOMERS_SQL.splitlines()]
        sql = " ".join(" ".join(lines).split())
        self.assertNotIn("max(name)", sql)
        self.assertIn("ORDER BY", sql)
        self.assertIn("created DESC", sql)

    def test_the_crm_is_still_only_ever_read(self):
        sql = crm.CUSTOMERS_SQL.lower()
        for writing in ("insert", "update", "delete ", "drop", "alter", "truncate"):
            self.assertNotIn(writing, sql)


class SavedListAudienceTests(Base):
    """A saved list has to mean what the screen said it meant.

    The Subscribers screen narrows by an audience and by the column filters and
    shows one count under both. Save stored only the filters, so a list saved
    while looking at "bought in the last 2 years, from Shopify" quietly meant
    everybody from Shopify. The only symptom would have been a send several
    times the size somebody expected.
    """

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()
        self.conn.execute("DELETE FROM segment")
        self.conn.commit()
        # Two shopify people, one of whom bought last month and one in 2015.
        for email, last in (("recent@x.nl", db.now()[:10]), ("oud@x.nl", "2015-03-02")):
            s = self.sub(email)
            self.conn.execute(
                "UPDATE subscriber SET source = 'shopify', spent = 900,"
                " last_order_at = ? WHERE id = ?", (last, s["id"]))
        # And somebody from the CRM who also bought last month.
        s = self.sub("crm@x.nl")
        self.conn.execute(
            "UPDATE subscriber SET source = 'crm', spent = 900,"
            " last_order_at = ? WHERE id = ?", (db.now()[:10], s["id"]))
        self.conn.commit()

    def saved(self, name="Test"):
        seg = self.conn.execute("SELECT * FROM segment WHERE name = ?",
                                (name,)).fetchone()
        return "list:%s" % seg["id"], seg

    def test_the_list_counts_what_the_screen_counted(self):
        on_screen = self.client.get(
            "/subscribers?audience=buyers_24m&source=shopify").get_data(as_text=True)
        self.assertIn("<b>1</b> people match this filter", on_screen)
        self.client.post("/subscribers/list/save",
                         data={"name": "Test", "source": "shopify",
                               "audience": "buyers_24m"})
        key, _seg = self.saved()
        self.assertEqual(sender.audience_count(self.conn, key), 1)

    def test_without_the_fix_it_would_have_been_wider(self):
        # The same filter with no audience really does mean two people, which is
        # what made the old behaviour silent rather than obviously broken.
        self.client.post("/subscribers/list/save",
                         data={"name": "Breed", "source": "shopify"})
        key, _seg = self.saved("Breed")
        self.assertEqual(sender.audience_count(self.conn, key), 2)

    def test_an_audience_on_its_own_can_now_be_saved(self):
        """The Save bar only appeared when a column filter was set, so this
        case had no way to be saved at all."""
        page = self.client.get("/subscribers?audience=buyers_24m").get_data(as_text=True)
        self.assertIn("Save as list", page)
        self.client.post("/subscribers/list/save",
                         data={"name": "Recent", "audience": "buyers_24m"})
        key, _seg = self.saved("Recent")
        self.assertEqual(sender.audience_count(self.conn, key), 2)

    def test_a_list_cannot_be_built_on_another_list(self):
        self.client.post("/subscribers/list/save",
                         data={"name": "Eerste", "source": "shopify"})
        key, _seg = self.saved("Eerste")
        self.client.post("/subscribers/list/save",
                         data={"name": "Tweede", "source": "crm", "audience": key})
        self.assertIsNone(self.conn.execute(
            "SELECT * FROM segment WHERE name = 'Tweede'").fetchone())

    def test_a_made_up_audience_is_ignored_not_stored(self):
        self.client.post("/subscribers/list/save",
                         data={"name": "Test", "source": "shopify",
                               "audience": "iets-verzonnen"})
        _key, seg = self.saved()
        self.assertEqual(seg["audience"], "")

    def test_a_saved_list_still_cannot_reach_somebody_who_opted_out(self):
        """The consent floor comes first and a list can only ever narrow it."""
        self.client.post("/subscribers/list/save",
                         data={"name": "Test", "source": "shopify",
                               "audience": "buyers_24m"})
        key, _seg = self.saved()
        row = self.conn.execute(
            "SELECT * FROM subscriber WHERE email = 'recent@x.nl'").fetchone()
        db.unsubscribe(self.conn, row["id"], by=db.BY_SELF)
        self.conn.commit()
        self.assertEqual(sender.audience_count(self.conn, key), 0)

    def test_lists_saved_before_this_existed_still_work(self):
        db.save_segment(self.conn, "Oud", {"source": "shopify"})
        seg = self.conn.execute(
            "SELECT * FROM segment WHERE name = 'Oud'").fetchone()
        self.assertEqual(seg["audience"], "")
        self.assertEqual(sender.audience_count(self.conn, "list:%s" % seg["id"]), 2)


class EngagementTests(Base):
    """Who is still reading, and who stopped.

    Opens were recorded per message, which answers "how did that campaign do"
    and cannot answer "should this person be on the next one". Mailing people
    who never open is the largest reason a shop's email starts landing in spam,
    after authentication.
    """

    def setUp(self):
        super().setUp()
        webapp.app.config["TESTING"] = True
        self.client = webapp.app.test_client()

    def mailed(self, email, times=1):
        """Actually send to this person, 0m0.000s 0m0.015s
0m0.000s 0m0.000s times, through the real path.

        One campaign per send, because the queue refuses to put the same person
        on the same campaign twice, and that refusal is worth leaving alone.
        """
        sub = self.sub(email)
        rows = []
        for n in range(times):
            camp = self.campaign()
            sender.queue(self.conn, camp["id"], "all")
            self.conn.execute("UPDATE campaign SET status = ? WHERE id = ?",
                              (db.SENDING, camp["id"]))
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM send WHERE campaign_id = ? AND to_email = ?",
                (camp["id"], email)).fetchone()
            with self.fake_smtp() as server:
                sender.send_one(self.conn, row, server)
            rows.append(row)
        return sub, rows

    def fresh(self, email):
        return self.conn.execute("SELECT * FROM subscriber WHERE email = ?",
                                 (email,)).fetchone()

    def test_sending_counts_on_the_person(self):
        self.mailed("klant@x.nl", times=2)
        row = self.fresh("klant@x.nl")
        self.assertEqual(row["sent_count"], 2)
        self.assertTrue(row["last_sent"])

    def test_an_open_lands_on_the_person(self):
        _sub, rows = self.mailed("klant@x.nl")
        self.assertIsNone(self.fresh("klant@x.nl")["last_opened"])
        sender.record_open(self.conn, rows[0]["token"])
        self.assertTrue(self.fresh("klant@x.nl")["last_opened"])

    def test_a_click_lands_on_the_person_and_counts_as_an_open(self):
        """Image blocking hides the pixel, so a click is often the only proof
        somebody read it at all."""
        _sub, rows = self.mailed("klant@x.nl")
        sender.record_click(self.conn, rows[0]["token"], "https://example.com/x")
        row = self.fresh("klant@x.nl")
        self.assertTrue(row["last_clicked"])
        self.assertTrue(row["last_opened"])

    def test_engaged_means_showed_interest_recently(self):
        _sub, rows = self.mailed("leest@x.nl")
        self.mailed("leest.niet@x.nl")
        self.assertEqual(sender.audience_count(self.conn, "engaged"), 0)
        sender.record_open(self.conn, rows[0]["token"])
        self.assertEqual(sender.audience_count(self.conn, "engaged"), 1)

    def test_an_open_long_ago_is_not_recent_interest(self):
        _sub, rows = self.mailed("ooit@x.nl")
        sender.record_open(self.conn, rows[0]["token"])
        self.conn.execute(
            "UPDATE subscriber SET last_opened = '2022-01-01T00:00:00',"
            " last_clicked = NULL WHERE email = 'ooit@x.nl'")
        self.conn.commit()
        self.assertEqual(sender.audience_count(self.conn, "engaged"), 0)

    def test_gone_quiet_is_three_unanswered_emails(self):
        """One can be a bad subject line and two can be a bad fortnight."""
        self.mailed("stil@x.nl", times=2)
        self.assertEqual(sender.audience_count(self.conn, "gone_quiet"), 0)
        self.mailed("stil@x.nl", times=1)
        self.assertEqual(sender.audience_count(self.conn, "gone_quiet"), 1)

    def test_one_open_ever_is_enough_to_not_be_gone_quiet(self):
        # Somebody who looked once chose to look. That is a different person
        # from one who has never opened anything.
        _sub, rows = self.mailed("keek.ooit@x.nl", times=3)
        self.assertEqual(sender.audience_count(self.conn, "gone_quiet"), 1)
        sender.record_open(self.conn, rows[0]["token"])
        self.assertEqual(sender.audience_count(self.conn, "gone_quiet"), 0)

    def test_both_new_audiences_still_sit_on_the_consent_floor(self):
        _sub, rows = self.mailed("weg@x.nl", times=3)
        sender.record_open(self.conn, rows[0]["token"])
        self.assertEqual(sender.audience_count(self.conn, "engaged"), 1)
        row = self.fresh("weg@x.nl")
        db.unsubscribe(self.conn, row["id"], by=db.BY_SELF)
        self.conn.commit()
        self.assertEqual(sender.audience_count(self.conn, "engaged"), 0)
        self.assertEqual(sender.audience_count(self.conn, "gone_quiet"), 0)

    def test_the_new_audiences_are_offered_and_explained(self):
        names = dict(sender.audiences(self.conn))
        self.assertIn("engaged", names)
        self.assertIn("gone_quiet", names)
        for key in ("engaged", "gone_quiet"):
            self.assertTrue(sender.AUDIENCE_NOTES.get(key), key)


class ScheduledCrmTests(Base):
    """The showroom sale that has to stop a flow.

    Most of this shop's money is taken in the showroom and a showroom sale
    lands in the CRM, never in Shopify. The mailer reads it off the subscriber
    row, and that row only ever changed when somebody pressed the import button
    by hand. So: abandon a basket on Wednesday, buy the sofa in Example City on
    Thursday, receive a discount code for it on Friday.
    """

    def setUp(self):
        super().setUp()
        # The run log outlives the tables Base drops, which is the whole point
        # of it, so it is cleared here rather than carried between tests.
        self.conn.execute("DELETE FROM ran")
        self.conn.commit()

    def test_a_job_is_due_the_first_time_and_not_again(self):
        self.assertTrue(db.due(self.conn, "crm_sync", 60))
        self.assertFalse(db.due(self.conn, "crm_sync", 60))

    def test_a_job_is_due_again_once_enough_time_has_passed(self):
        db.due(self.conn, "crm_sync", 60)
        self.conn.execute("UPDATE ran SET at = ? WHERE job = 'crm_sync'",
                          (db.minutes_ago(120),))
        self.conn.commit()
        self.assertTrue(db.due(self.conn, "crm_sync", 60))

    def test_asking_and_recording_happen_together(self):
        """Two calls would leave room for the job to fail in between and never
        be retried, or for two workers to record it and run it neither time."""
        self.assertTrue(db.due(self.conn, "a", 60))
        row = self.conn.execute("SELECT * FROM ran WHERE job = 'a'").fetchone()
        self.assertTrue(row["at"])

    def test_jobs_do_not_share_a_clock(self):
        self.assertTrue(db.due(self.conn, "crm_sync", 60))
        self.assertTrue(db.due(self.conn, "iets_anders", 60))

    def test_the_run_reads_the_crm_before_it_polls_or_sends(self):
        import cron
        names = []

        def remember(name, fn, conn):
            names.append(name)

        with mock.patch.object(cron, "step", remember):
            cron.main()
        self.assertIn("crm", names)
        self.assertLess(names.index("crm"), names.index("poll"))
        self.assertLess(names.index("crm"), names.index("send"))

    def test_it_does_nothing_when_there_is_no_crm_to_read(self):
        import cron
        with mock.patch.object(cron.crm, "configured", return_value=False):
            with mock.patch.object(cron.crm, "sync") as synced:
                self.assertEqual(cron._crm(self.conn), "not configured")
        synced.assert_not_called()

    def test_it_does_not_re_read_the_crm_every_quarter_of_an_hour(self):
        import cron
        with mock.patch.object(cron.crm, "configured", return_value=True):
            with mock.patch.object(cron.crm, "sync",
                                   return_value={"matched": 1}) as synced:
                cron._crm(self.conn)
                cron._crm(self.conn)
                cron._crm(self.conn)
        self.assertEqual(synced.call_count, 1)

    def test_the_crm_sync_only_ever_writes_here(self):
        # It reads Postgres and writes SQLite. Nothing in it can change the CRM.
        import inspect
        src = inspect.getsource(crm.sync)
        self.assertIn("conn.execute", src)
        self.assertNotIn("con.run(", src.split("spend_by_email")[-1])


@unittest.skipUnless(config.SHOPIFY_STORE_DOMAIN,
    "needs a Shopify store configured; these passed before only because\n"
    "live credentials happened to be sitting in a .env file next door")
class PostPurchaseTests(Base):
    """Day 30, day 60, day 90, and the one thing that must never happen.

    The reason this has its own trigger instead of the win back one set to 30
    days: the win back poll takes anybody whose last order is older than the
    setting, five hundred at a time. At 30 days that is every customer the shop
    has ever had, and switching it on would send five hundred of them the same
    email within the quarter hour, from a sending domain a fortnight old.
    """

    def setUp(self):
        super().setUp()
        for t in ("flow_send", "flow", "flow_step", "flow_def"):
            self.conn.execute("DELETE FROM " + t)
        self.conn.commit()
        db.seed_flows(self.conn)
        config.FLOWS_ENABLED = True
        # Far enough back that a purchase a month ago is inside it. The floor
        # itself gets its own test below.
        config.FLOWS_FROM = "2020-01-01"
        self.d = flows.find(self.conn, "post-purchase")

    def buyer(self, email, days_ago):
        sub = self.sub(email)
        when = (datetime.datetime.now(datetime.timezone.utc)
                - datetime.timedelta(days=days_ago)).date().isoformat()
        self.conn.execute(
            "UPDATE subscriber SET spent = 1495, orders = 1, last_order_at = ?"
            " WHERE id = ?", (when, sub["id"]))
        self.conn.commit()
        return sub

    def live(self):
        self.conn.execute("UPDATE flow_def SET status = ?, days = 30"
                          " WHERE id = ?", (flows.LIVE, self.d["id"]))
        self.conn.commit()
        self.d = flows.find(self.conn, "post-purchase")

    def entered(self):
        return [r["email"] for r in self.conn.execute(
            "SELECT email FROM flow WHERE def_id = ?", (self.d["id"],))]

    # --- the guarantee ------------------------------------------------------

    def test_it_cannot_reach_the_back_catalogue(self):
        """This is the whole reason it is a separate trigger. Everybody's last
        order is more than thirty days ago, so a win back flow set to 30 days
        would enter five hundred of them on the first run."""
        config.FLOWS_FROM = "2026-09-01"
        self.buyer("oud@x.nl", days_ago=900)
        self.buyer("ouder@x.nl", days_ago=2000)
        self.buyer("vorige.maand@x.nl", days_ago=45)
        self.live()
        flows.poll(self.conn)
        self.assertEqual(self.entered(), [])

    def test_the_win_back_trigger_really_would_have_swept_them_all_in(self):
        """Not a hypothetical. The same three people, the same 30 days, the
        other trigger."""
        config.FLOWS_FROM = "2026-09-01"
        self.buyer("oud@x.nl", days_ago=900)
        self.buyer("ouder@x.nl", days_ago=2000)
        self.conn.execute(
            "UPDATE flow_def SET trigger = 'winback', status = ?, days = 30"
            " WHERE id = ?", (flows.LIVE, self.d["id"]))
        self.conn.commit()
        self.d = flows.find(self.conn, "post-purchase")
        flows.poll(self.conn)
        self.assertEqual(sorted(self.entered()), ["oud@x.nl", "ouder@x.nl"])

    def test_somebody_who_bought_after_the_flows_went_on_does_enter(self):
        self.buyer("nieuw@x.nl", days_ago=31)
        self.live()
        flows.poll(self.conn)
        self.assertEqual(self.entered(), ["nieuw@x.nl"])

    def test_somebody_who_bought_yesterday_waits(self):
        self.buyer("gisteren@x.nl", days_ago=1)
        self.live()
        flows.poll(self.conn)
        self.assertEqual(self.entered(), [])

    def test_it_is_seeded_paused_so_a_deploy_never_starts_it(self):
        self.assertEqual(self.d["status"], flows.DRAFT)
        self.assertEqual(self.d["days"], 30)

    def test_the_clock_starts_at_the_purchase_not_at_the_poll(self):
        """Somebody found five days late is five days into the sequence, not at
        the beginning of it. Otherwise a restart replays everybody's day one."""
        self.buyer("laat@x.nl", days_ago=35)
        self.live()
        flows.poll(self.conn)
        row = self.conn.execute(
            "SELECT abandoned_at FROM flow WHERE email = 'laat@x.nl'").fetchone()
        self.assertLess(row["abandoned_at"], db.now())
        self.assertGreater(row["abandoned_at"], db.minutes_ago(60 * 24 * 6))

    def test_one_purchase_enters_once_however_often_it_is_polled(self):
        self.buyer("een@x.nl", days_ago=31)
        self.live()
        for _ in range(3):
            flows.poll(self.conn)
        self.assertEqual(self.entered(), ["een@x.nl"])

    def test_buying_again_stops_the_sequence(self):
        self.assertTrue(flows.TRIGGERS["bought"]["stops_on_order"])

    def test_the_checkout_flow_still_outranks_it(self):
        """Somebody who is standing at your checkout hears about that and
        nothing else."""
        self.assertLess(flows.TRIGGERS["bought"]["priority"],
                        flows.TRIGGERS["checkout"]["priority"])
        self.assertLess(flows.TRIGGERS["bought"]["priority"],
                        flows.TRIGGERS["addtocart"]["priority"])
        self.assertGreater(flows.TRIGGERS["bought"]["priority"],
                           flows.TRIGGERS["winback"]["priority"])

    # --- the emails ---------------------------------------------------------

    def test_there_are_three_of_them_a_month_apart(self):
        steps = flows.steps_of(self.conn, self.d["id"])
        self.assertEqual(len(steps), 3)
        self.assertEqual([s["hours"] for s in steps], [0, 24 * 30, 24 * 30])

    def test_only_the_last_one_carries_a_code(self):
        """A discount 60 days after somebody paid full price teaches them to
        wait next time."""
        codes = [(s["offer_code"] or "").strip()
                 for s in flows.steps_of(self.conn, self.d["id"])]
        self.assertEqual(codes, ["", "", discounts.AUTO])

    def test_the_code_it_asks_for_is_worth_what_it_says(self):
        step = flows.steps_of(self.conn, self.d["id"])[2]
        rate, why = discounts.rate_for(flows._promise_text(step))
        self.assertEqual(why, "")
        self.assertEqual(rate, 0.10)

    def test_it_says_why_they_are_receiving_it(self):
        """They bought. That is the existing-customer basis, and it is not the
        same as saying they signed up for a newsletter."""
        self.assertEqual(flows.TRIGGERS["bought"]["reason"], "customer")

    def test_every_one_of_them_renders(self):
        for pos in range(3):
            subject, html = flows.preview(self.conn, self.d, pos)
            self.assertTrue(subject)
            self.assertIn("Afmelden", html)
            self.assertNotIn("%(naam)s", html)
            self.assertNotIn("{{naam}}", html)


