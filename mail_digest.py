#!/usr/bin/env python3
"""Summarize yesterday's work email (read-only IMAP) with a local Ollama model,
grouping messages into threads.

- New thread (started on the digest day): one summary of the whole thread.
- Existing thread (has prior history): a short recap + what each new message adds.

Read-only: uses EXAMINE + BODY.PEEK, never alters flags or messages.
Password: systemd credential, then ~/.config/mail-digest/imap-password (0600), then keyring.
Folders + options: ~/.config/mail-digest/config.toml
"""
import argparse, email, html, imaplib, json, os, re, smtplib, subprocess, sys, time, urllib.request
from email.message import EmailMessage
from datetime import datetime, timedelta
from email.header import decode_header, make_header

HOST = "mail.igalia.com"
PORT = 993
USER = "pmatos"
OLLAMA = "http://127.0.0.1:11434/api/chat"
MODEL = "gemma4:26b-a4b-it-q4_K_M"
MAXCHARS = 3000          # per-message body sent to the model
THREAD_LOOKBACK = 60     # days of header history for thread context
RECAP_PRIOR_MAX = 6      # prior messages fed into a recap
PRIORITY = {"ACTION": 2, "FYI": 1, "AUTO": 0}

CONFIG_DIR = os.path.expanduser("~/.config/mail-digest")
CONFIG_FILE = os.path.expanduser(os.environ.get("MAIL_DIGEST_CONFIG",
                                                 "~/.config/mail-digest/config.toml"))
RE_PREFIX = re.compile(r"^\s*((re|fwd|fw|aw|sv)\s*:\s*)+", re.I)
UIDRE = re.compile(rb"UID (\d+)")
IDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
MIDRE = re.compile(r"<[^>]+>")


# ---------- credentials / config ----------
def get_secret(name):
    """Fetch a secret by name from, in order: systemd credential, 0600 file,
    user systemd-creds .cred, keyring (imap only)."""
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred and os.path.exists(os.path.join(cred, name)):
        return open(os.path.join(cred, name)).read().strip()
    plain = os.path.join(CONFIG_DIR, name)
    if os.path.exists(plain):
        return open(plain).read().strip()
    enc = plain + ".cred"
    if os.path.exists(enc):
        try:
            return subprocess.check_output(
                ["systemd-creds", "--user", "decrypt", "--name", name, enc, "-"],
                stderr=subprocess.DEVNULL).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    if name == "imap-password":
        try:
            return subprocess.check_output(
                ["secret-tool", "lookup", "service", "imap", "host", HOST, "user", USER],
                stderr=subprocess.DEVNULL).decode().strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return None


def get_password():
    pw = get_secret("imap-password")
    if not pw:
        sys.exit("No IMAP password found. Create it with:\n"
                 "  ( umask 177; systemd-ask-password 'Igalia IMAP password:' "
                 "> ~/.config/mail-digest/imap-password )")
    return pw


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    import tomllib
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        sys.exit(f"Error reading config {CONFIG_FILE}: {e}")


def connect():
    last = None
    for attempt in range(5):                 # 7am: network may still be warming up
        try:
            M = imaplib.IMAP4_SSL(HOST, PORT, timeout=30)
            M.login(USER, get_password())
            return M
        except (OSError, imaplib.IMAP4.error) as e:
            last = e
            time.sleep(5)
    raise last


def md_to_html(md):
    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(?!\s)(.+?)\*", r"<em>\1</em>", t)
        return t
    out, inlist = ['<div style="font-family:system-ui,sans-serif;max-width:720px;line-height:1.5">'], False
    for line in md.splitlines():
        if line.startswith("### "):
            if inlist: out.append("</ul>"); inlist = False
            out.append(f"<h3 style='margin:.8em 0 .2em'>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            if inlist: out.append("</ul>"); inlist = False
            out.append(f"<h2 style='border-bottom:1px solid #ddd;padding-bottom:.2em'>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            if inlist: out.append("</ul>"); inlist = False
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.startswith("- "):
            if not inlist: out.append("<ul>"); inlist = True
            out.append(f"<li>{inline(line[2:])}</li>")
        elif not line.strip():
            if inlist: out.append("</ul>"); inlist = False
        else:
            if inlist: out.append("</ul>"); inlist = False
            out.append(f"<p style='margin:.2em 0'>{inline(line)}</p>")
    if inlist:
        out.append("</ul>")
    out.append("</div>")
    return "\n".join(out)


def send_email(cfg, subject, md_text):
    d = cfg.get("delivery", {})
    host, port = d.get("smtp_host", "mail.igalia.com"), int(d.get("smtp_port", 465))
    mail_from = d.get("mail_from", f"{USER}@igalia.com")
    mail_to = d.get("mail_to")
    smtp_user = d.get("smtp_user", USER)
    if not mail_to:
        sys.exit("config [delivery].mail_to is required for --send")
    pw = get_secret("smtp-password") or get_secret("imap-password")
    if not pw:
        sys.exit("No SMTP password. Create it with:\n"
                 "  ( umask 177; systemd-ask-password 'Igalia SMTP password:' "
                 "> ~/.config/mail-digest/smtp-password )")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, mail_from, mail_to
    msg.set_content(md_text)
    msg.add_alternative(md_to_html(md_text), subtype="html")
    with smtplib.SMTP_SSL(host, port, timeout=60) as s:
        s.login(smtp_user, pw)
        s.send_message(msg)


# ---------- text helpers ----------
def dstr(s):
    try:
        s = str(make_header(decode_header(s))) if s else ""
    except Exception:
        s = s or ""
    s = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def clean_subject(s):
    return RE_PREFIX.sub("", s or "").strip() or "(no subject)"


def display_name(frm):
    return dstr(re.sub(r"\s*<[^>]+>\s*", "", frm or "")).strip(' "') or frm


def strip_html(t):
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    return html.unescape(t)


def clean_body(msg):
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                break
        if not text:
            for part in msg.walk():
                if part.get_content_type() == "text/html":
                    raw = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
                    text = strip_html(raw)
                    break
    else:
        raw = msg.get_payload(decode=True)
        text = raw.decode(msg.get_content_charset() or "utf-8", "replace") if raw else ""
        if msg.get_content_type() == "text/html":
            text = strip_html(text)
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if re.match(r"^On .*wrote:$", s) or s.startswith("-----Original Message-----") \
           or s.startswith("________") or s == "-- ":
            break
        if s.startswith(">"):
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()[:MAXCHARS]


# ---------- ollama ----------
def ollama(prompt, num_predict):
    data = json.dumps({"model": MODEL,
                       "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "think": False,
                       "options": {"temperature": 0.2, "num_predict": num_predict}}).encode()
    req = urllib.request.Request(OLLAMA, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["message"]["content"].strip()


def parse_cat(out):
    cat, _, summ = out.partition("|")
    cat = (cat.strip().upper().split() or [""])[0]
    if cat not in PRIORITY:
        return ("FYI", out.strip())
    return (cat, summ.strip() or "(no summary)")


CATS = "ACTION (pmatos must act/reply), FYI (informational), AUTO (automated notification, CI, mailing-list/vote bot, newsletter)"


def summ_new_thread(msgs):
    convo = "\n\n----\n".join(
        f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body'] or ''}" for m in msgs)[:6000]
    p = (f"You triage a NEW work email thread (started today, {len(msgs)} message(s)) for a "
         "busy compiler engineer (pmatos).\n"
         "Reply EXACTLY one line: CATEGORY | 1-2 sentence summary of the whole thread and any "
         "action pmatos must take (with deadlines).\n"
         f"CATEGORY is one of: {CATS}.\n\n{convo}\n")
    return parse_cat(ollama(p, 220))


def recap_prior(msgs):
    ctx = "\n\n----\n".join(
        f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body'] or ''}" for m in msgs)[:5000]
    p = ("In 1-2 factual sentences, recap what this email thread has been about so far "
         "(context prior to today). No preamble, no invented details.\n\n" + ctx)
    return ollama(p, 130)


def summ_added(recap, m):
    p = ("Ongoing email thread context: " + (recap or "(earlier messages not available)") +
         "\n\nA new reply arrived today. Reply EXACTLY one line: CATEGORY | one sentence on what "
         "this message adds or changes, plus any action for pmatos.\n"
         f"CATEGORY is one of: {CATS}.\n\n"
         f"From: {m['from']}\nSubject: {m['subject']}\n\n{m['body'] or ''}\n")
    return parse_cat(ollama(p, 130))


# ---------- IMAP fetch ----------
def parse_internaldate(info):
    m = IDATE.search(info)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def refs_of(hmsg):
    ids, seen = [], set()
    for h in ("References", "In-Reply-To"):
        v = hmsg.get(h)
        if v:
            for i in MIDRE.findall(v):
                if i not in seen:
                    seen.add(i)
                    ids.append(i)
    return ids


def fetch_headers(M, folder, since_s, before_s):
    typ, _ = M.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        print(f"!! cannot open folder {folder}", file=sys.stderr)
        return []
    typ, data = M.uid("SEARCH", None, "SINCE", since_s, "BEFORE", before_s)
    uids = data[0].split() if data and data[0] else []
    if not uids:
        return []
    typ, data = M.uid("FETCH", b",".join(uids),
        "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT FROM)])")
    recs = []
    for item in data:
        if not isinstance(item, tuple):
            continue
        info, payload = item
        um = UIDRE.search(info)
        h = email.message_from_bytes(payload)
        recs.append({
            "mid": (h.get("Message-ID") or "").strip(),
            "folder": folder, "uid": um.group(1) if um else None,
            "from": dstr(h.get("From")),
            "subject": dstr(h.get("Subject")) or "(no subject)",
            "date": parse_internaldate(info),
            "refs": refs_of(h),
            "body": None,
        })
    return recs


def fetch_bodies(M, need):
    bodies = {}
    for folder, uids in need.items():
        typ, _ = M.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            continue
        typ, data = M.uid("FETCH", b",".join(sorted(uids)), "(UID BODY.PEEK[])")
        for item in data:
            if not isinstance(item, tuple):
                continue
            info, payload = item
            um = UIDRE.search(info)
            if not um:
                continue
            bodies[(folder, um.group(1))] = clean_body(email.message_from_bytes(payload))
    return bodies


# ---------- threading ----------
class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        root = x
        while self.p[root] != root:
            root = self.p[root]
        while self.p[x] != root:
            self.p[x], x = root, self.p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


def build_threads(recs):
    # dedup by Message-ID (same message filed in multiple folders)
    dedup, i = {}, 0
    for r in recs:
        key = r["mid"] or f"__nomid_{i}"
        if key not in dedup:
            dedup[key] = r
        i += 1
    records = list(dedup.values())
    mids_present = {r["mid"] for r in records if r["mid"]}

    uf = UF()
    for r in records:
        node = r["mid"] or f"__u_{id(r)}"
        r["_node"] = node
        uf.find(node)
        for ref in r["refs"]:
            uf.union(node, ref)

    groups = {}
    for r in records:
        groups.setdefault(uf.find(r["_node"]), []).append(r)
    return list(groups.values()), mids_present


# ---------- main ----------
def main():
    global MODEL, THREAD_LOOKBACK
    ap = argparse.ArgumentParser()
    ap.add_argument("--list-folders", action="store_true")
    ap.add_argument("--folders", default=None, help="comma-separated; overrides config")
    ap.add_argument("--day", default="yesterday", help="'yesterday' or YYYY-MM-DD")
    ap.add_argument("--send", action="store_true", help="email the digest instead of printing")
    a = ap.parse_args()

    cfg = load_config()
    if cfg.get("model"):
        MODEL = cfg["model"]
    THREAD_LOOKBACK = int(cfg.get("thread_lookback_days", THREAD_LOOKBACK))
    if a.folders is not None:
        folders = [f.strip() for f in a.folders.split(",") if f.strip()]
    else:
        folders = cfg.get("folders") or ["INBOX"]

    M = connect()
    try:
        if a.list_folders:
            typ, data = M.list()
            for raw in data:
                line = raw.decode(errors="replace")
                m = re.match(r'\([^)]*\)\s+"?[^"]*"?\s+(.+)$', line)
                print((m.group(1).strip().strip('"') if m else line))
            return

        now = datetime.now().astimezone()
        localtz = now.tzinfo
        anchor = (now - timedelta(days=1)).date() if a.day == "yesterday" \
            else datetime.strptime(a.day, "%Y-%m-%d").date()
        before = datetime(anchor.year, anchor.month, anchor.day) + timedelta(days=1)
        since = before - timedelta(days=THREAD_LOOKBACK)
        since_s, before_s = since.strftime("%d-%b-%Y"), before.strftime("%d-%b-%Y")
        day_label = anchor.strftime("%A %Y-%m-%d")

        def is_new(r):
            return r["date"] is not None and r["date"].astimezone(localtz).date() == anchor

        recs = []
        for folder in folders:
            got = fetch_headers(M, folder, since_s, before_s)
            recs += got
            print(f"  · scanned {folder}: {len(got)} msgs (60d)", file=sys.stderr)

        threads, mids_present = build_threads(recs)
        active = [t for t in threads if any(is_new(r) for r in t)]
        print(f"  · {len(active)} active thread(s) with new mail on {day_label}", file=sys.stderr)

        # decide bodies to fetch: all new msgs + (capped) prior for existing threads
        need = {}
        plans = []
        for t in active:
            new_msgs = sorted([r for r in t if is_new(r)], key=lambda r: r["date"])
            prior = sorted([r for r in t if not is_new(r)], key=lambda r: r["date"])
            missing_parent = any(ref not in mids_present for r in new_msgs for ref in r["refs"])
            existing = bool(prior) or missing_parent
            fetch_set = list(new_msgs)
            if existing and prior:
                fetch_set += prior[-RECAP_PRIOR_MAX:]
            for r in fetch_set:
                if r["uid"]:
                    need.setdefault(r["folder"], set()).add(r["uid"])
            plans.append((t, new_msgs, prior, existing))

        bodies = fetch_bodies(M, need)
        for t in active:
            for r in t:
                r["body"] = bodies.get((r["folder"], r["uid"]), "")

        results = []
        for t, new_msgs, prior, existing in plans:
            subject = clean_subject(sorted(t, key=lambda r: r["date"] or now)[0]["subject"])
            names, seen = [], set()
            for r in sorted(t, key=lambda r: r["date"] or now):
                nm = display_name(r["from"])
                if nm not in seen:
                    seen.add(nm)
                    names.append(nm)
            participants = ", ".join(names[:4]) + (" …" if len(names) > 4 else "")
            latest = max((r["date"] for r in new_msgs if r["date"]), default=now)
            print(f"  · summarizing: {subject[:55]}", file=sys.stderr)

            if not existing:
                cat, summary = summ_new_thread(new_msgs)
                results.append({"cat": cat, "kind": "new", "subject": subject,
                                "participants": participants, "summary": summary,
                                "recap": None, "adds": [], "latest": latest})
            else:
                recap = recap_prior(prior[-RECAP_PRIOR_MAX:]) if prior else None
                adds, cats = [], []
                for m in new_msgs:
                    c, txt = summ_added(recap, m)
                    adds.append((display_name(m["from"]), txt))
                    cats.append(c)
                cat = max(cats, key=lambda c: PRIORITY[c]) if cats else "FYI"
                results.append({"cat": cat, "kind": "existing", "subject": subject,
                                "participants": participants, "summary": None,
                                "recap": recap, "adds": adds, "latest": latest})

        text = format_digest(results, day_label)
        subject = f"Work digest — {day_label}  ({len(results)} threads)"
        if a.send:
            send_email(cfg, subject, text)
            print(f"sent '{subject}' to {cfg.get('delivery', {}).get('mail_to')}", file=sys.stderr)
        else:
            print(text)
    finally:
        try:
            M.logout()
        except Exception:
            pass


def format_digest(threads, day):
    if not threads:
        return f"# Work email digest — {day}\n\n(no mail found in the selected folders)"
    groups = [("ACTION", "## Needs action / reply"),
              ("FYI", "## FYI"),
              ("AUTO", "## Automated / low priority")]
    out = [f"# Work email digest — {day}  ({len(threads)} threads)", ""]
    for cat, header in groups:
        g = sorted([t for t in threads if t["cat"] == cat], key=lambda t: t["latest"], reverse=True)
        if not g:
            continue
        out.append(f"{header}  ({len(g)})")
        out.append("")
        for t in g:
            out.append(f"### {t['subject']}")
            out.append(f"*{t['participants']}*")
            if t["kind"] == "new":
                out.append(f"🆕 {t['summary']}")
            else:
                out.append(f"↳ _Recap:_ {t['recap']}" if t["recap"]
                           else "↳ _Continuation of an earlier thread (prior messages outside the 60-day window)._")
                for who, txt in t["adds"]:
                    out.append(f"- **{who}**: {txt}")
            out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    main()
