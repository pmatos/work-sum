import email, os, sys, unittest
from datetime import date, datetime, timezone
from email.message import EmailMessage

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import mail_digest as md


def plain(body, charset="utf-8"):
    m = EmailMessage()
    m.set_content(body, charset=charset)
    return m


class CleanBody(unittest.TestCase):
    def test_bottom_posted_reply_is_kept(self):
        body = ("On 2026-09-21 05:19, Alice wrote:\n> Big news today.\n> More quoted text.\n\n"
                "Congratulations everyone, great milestone!\n")
        self.assertEqual(md.clean_body(plain(body)), "Congratulations everyone, great milestone!")

    def test_inline_reply_keeps_all_answers(self):
        body = ("On Mon, Bob wrote:\n> Question one?\nAnswer one.\n> Question two?\nAnswer two.\n")
        self.assertEqual(md.clean_body(plain(body)), "Answer one.\nAnswer two.")

    def test_top_posted_with_wrapped_attribution(self):
        body = ("Sounds good, ship it.\n\nOn Mon, Sep 21, 2026 at 5:19 AM Alice Example <\n"
                "alice@example.com> wrote:\n> old stuff\n")
        self.assertEqual(md.clean_body(plain(body)), "Sounds good, ship it.")

    def test_spanish_attribution(self):
        body = "De acuerdo.\n\nEl lun, 21 sept 2026 a las 10:00, Ana escribió:\n> hola\n"
        self.assertEqual(md.clean_body(plain(body)), "De acuerdo.")

    def test_outlook_history_is_cut(self):
        body = ("Approved.\n\nFrom: Carol <carol@example.com>\nSent: Monday\nTo: team\n"
                "Subject: budget\n\nPlease approve the budget.\n")
        self.assertEqual(md.clean_body(plain(body)), "Approved.")

    def test_forwarded_headers_are_kept(self):
        body = ("FYI, see below.\n---------- Forwarded message ---------\nFrom: Dan\n"
                "Date: Mon\nSubject: x\n\nForwarded content.\n")
        self.assertIn("Forwarded content.", md.clean_body(plain(body)))

    def test_signature_is_cut(self):
        self.assertEqual(md.clean_body(plain("Hello.\n-- \nAlice\nCEO\n")), "Hello.")

    def test_html_only_with_gmail_quote(self):
        m = EmailMessage()
        m.set_content("<p>First line</p><p>Second line</p>"
                      '<div class="gmail_quote">On x wrote:<blockquote>old</blockquote></div>',
                      subtype="html")
        self.assertEqual(md.clean_body(m), "First line\nSecond line")

    def test_unknown_charset_does_not_crash(self):
        raw = (b"Content-Type: text/plain; charset=unknown-8bit\n\nHello \xe9t\xe9\n")
        self.assertIn("Hello", md.clean_body(email.message_from_bytes(raw)))


def rec(mid, day, refs=(), irt=None, from_me=False):
    return {"mid": mid, "folder": "INBOX", "uid": mid.strip("<>"), "from": "X <x@example.com>",
            "subject": "s", "date": datetime(2026, 9, day, 12, tzinfo=timezone.utc),
            "refs": list(refs), "irt": irt, "from_me": from_me, "spam": False,
            "to_me": False, "cc_me": False, "list": "", "auto": False, "body": ""}


class Threading(unittest.TestCase):
    def test_gitlab_new_issue_is_not_ongoing(self):
        r = rec("<issue_1@gitlab>", 22, refs=["<reply-abc@gitlab>", "<issue_1@gitlab>"])
        threads, present = md.build_threads([r])
        self.assertEqual(len(threads), 1)
        self.assertFalse(md.is_ongoing([], [r], present))

    def test_reply_to_missing_parent_is_ongoing(self):
        r = rec("<b@x>", 22, refs=["<a@x>"], irt="<a@x>")
        _, present = md.build_threads([r])
        self.assertTrue(md.is_ongoing([], [r], present))

    def test_reply_chain_forms_one_thread(self):
        a = rec("<a@x>", 20)
        b = rec("<b@x>", 22, refs=["<a@x>"], irt="<a@x>")
        c = rec("<c@x>", 22, refs=["<a@x>", "<b@x>"], irt="<b@x>")
        threads, present = md.build_threads([a, b, c])
        self.assertEqual(len(threads), 1)
        self.assertTrue(md.is_ongoing([a], [b, c], present))

    def test_same_message_in_two_folders_is_deduplicated(self):
        a, b = rec("<a@x>", 22), rec("<a@x>", 22)
        b["folder"] = "lists"
        threads, _ = md.build_threads([a, b])
        self.assertEqual(sum(len(t) for t in threads), 1)


class Headers(unittest.TestCase):
    def hdr(self, **kw):
        m = EmailMessage()
        for k, v in kw.items():
            m[k.replace("_", "-")] = v
        return m

    def test_spam(self):
        self.assertTrue(md.is_spam(self.hdr(X_Spam_Report="YES, Score=6.8")))
        self.assertTrue(md.is_spam(self.hdr(X_Spam_Flag="YES")))
        self.assertFalse(md.is_spam(self.hdr(X_Spam_Report="NO, Score=-95")))

    def test_auto(self):
        self.assertTrue(md.is_auto(self.hdr(Auto_Submitted="auto-generated")))
        self.assertTrue(md.is_auto(self.hdr(Precedence="bulk")))
        self.assertFalse(md.is_auto(self.hdr(Precedence="list")))

    def test_record_addressing(self):
        payload = (b"From: Bob <bob@example.com>\r\nTo: Team <team@example.com>\r\n"
                   b"Cc: Jane Doe <JDoe@example.com>\r\nMessage-ID: <m@x>\r\n"
                   b"In-Reply-To: <p@x>\r\nSubject: =?utf-8?q?Caf=C3=A9?=\r\n\r\n")
        r = md.make_record("INBOX", b'1 (UID 42 INTERNALDATE "22-Sep-2026 10:00:00 +0200"', payload,
                           {"jdoe@example.com"})
        self.assertEqual((r["uid"], r["irt"], r["subject"]), (b"42", "<p@x>", "Café"))
        self.assertTrue(r["cc_me"])
        self.assertFalse(r["to_me"] or r["from_me"])

    def test_trailing_uid_after_literal(self):
        data = [(b'1 (INTERNALDATE "22-Sep-2026 10:00:00 +0200" BODY[HEADER] {3}', b"x\r\n"),
                b" UID 7)"]
        (meta, _), = md.iter_fetch(data)
        self.assertEqual(md.UIDRE.search(meta).group(1), b"7")


class CatchUp(unittest.TestCase):
    Y = date(2026, 9, 22)

    def test_no_state_sends_yesterday(self):
        self.assertEqual(md.days_to_send(None, self.Y), ([self.Y], 0))

    def test_up_to_date(self):
        self.assertEqual(md.days_to_send(self.Y, self.Y), ([], 0))

    def test_fills_gap(self):
        days, skipped = md.days_to_send(date(2026, 9, 19), self.Y)
        self.assertEqual(days, [date(2026, 9, 20), date(2026, 9, 21), self.Y])
        self.assertEqual(skipped, 0)

    def test_caps_long_gap(self):
        days, skipped = md.days_to_send(date(2026, 8, 31), self.Y, cap=7)
        self.assertEqual(len(days), 7)
        self.assertEqual(days[0], date(2026, 9, 16))
        self.assertEqual(skipped, 15)


class Rendering(unittest.TestCase):
    def test_fit_keeps_newest(self):
        msgs = [dict(rec(f"<{i}@x>", 22), body="b" * 3000) for i in range(10)]
        out = md.fit(msgs, 4000)
        self.assertIn("earlier message(s) omitted", out)
        self.assertLessEqual(out.count("From:"), 5)

    def test_markdown_emphasis_in_html(self):
        out = md.md_to_html("↳ *Earlier:* recap\n**New:** text\n- item")
        self.assertIn("<em>Earlier:</em>", out)
        self.assertIn("<strong>New:</strong>", out)
        self.assertIn("<li>item</li>", out)

    def test_digest_groups_and_overview(self):
        base = {"ongoing": False, "recap": None, "participants": "A", "folders": "INBOX",
                "n_new": 1, "sender": "A", "latest": datetime(2026, 9, 22, tzinfo=timezone.utc)}
        res = [dict(base, cat="ACTION", subject="Review", summary="Review by Friday."),
               dict(base, cat="JUNK", subject="Buy now", summary="spam")]
        text = md.format_digest(res, "Tuesday 2026-09-22", spam_skipped=2)
        self.assertIn("**1 need action · 1 probably junk**", text)
        self.assertIn("- Buy now — A", text)
        self.assertIn("2 message(s) flagged as spam", text)


if __name__ == "__main__":
    unittest.main()
