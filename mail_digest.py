#!/usr/bin/env python3
"""work-sum: summarize a day's work email (read-only IMAP) with a local Ollama model,
grouping messages into threads.

- New thread (started on the digest day): one summary of the whole thread.
- Ongoing thread: a short recap of earlier messages + what the day's messages add.

Read-only: uses EXAMINE + BODY.PEEK, never alters flags or messages.
Config: ~/.config/mail-digest/config.toml (see config.example.toml).
"""
import argparse, email, html, imaplib, json, os, re, smtplib, subprocess, sys, time, traceback, urllib.request
from datetime import date, datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import getaddresses, parseaddr

OLLAMA = "http://127.0.0.1:11434/api/chat"
DEFAULT_MODEL = "gemma4:26b-a4b-it-q4_K_M"
MAXCHARS = 3000          # per-message body cap
NEW_BUDGET = 8000        # chars of the day's messages per thread sent to the model
PRIOR_BUDGET = 5000      # chars of earlier messages used for the recap
RECAP_PRIOR_MAX = 6      # earlier messages fetched for the recap
CATCHUP_MAX_DAYS = 7
CATS = ("ACTION", "FYI", "AUTO", "JUNK")
PRIORITY = {"ACTION": 3, "FYI": 2, "AUTO": 1, "JUNK": 0}

CONFIG_DIR = os.path.expanduser("~/.config/mail-digest")
CONFIG_FILE = os.path.expanduser(os.environ.get("MAIL_DIGEST_CONFIG",
                                                 os.path.join(CONFIG_DIR, "config.toml")))
STATE_FILE = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                          "mail-digest", "last-sent")

RE_PREFIX = re.compile(r"^\s*((re|fwd|fw|aw|sv|rv|enc)\s*:\s*)+", re.I)
UIDRE = re.compile(rb"UID (\d+)")
IDATE = re.compile(rb'INTERNALDATE "([^"]+)"')
MIDRE = re.compile(r"<[^>]+>")
HDR_FIELDS = ("MESSAGE-ID IN-REPLY-TO REFERENCES SUBJECT FROM TO CC LIST-ID "
              "AUTO-SUBMITTED PRECEDENCE X-SPAM-FLAG X-SPAM-STATUS X-SPAM-REPORT")


def log(msg):
    print(f"  · {msg}", file=sys.stderr)


class DeliveryError(Exception):
    pass


# ---------- config / credentials ----------
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    import tomllib
    try:
        with open(CONFIG_FILE, "rb") as f:
            return tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        sys.exit(f"Error reading config {CONFIG_FILE}: {e}")


class Settings:
    def __init__(self, cfg):
        acct = cfg.get("account", {})
        self.host, self.user = acct.get("imap_host"), acct.get("imap_user")
        if not self.host or not self.user:
            sys.exit(f"config [account] needs imap_host and imap_user ({CONFIG_FILE})")
        self.port = int(acct.get("imap_port", 993))
        ident = cfg.get("identity", {})
        self.name = ident.get("name") or self.user
        self.role = ident.get("role", "")
        self.me = {a.lower() for a in ident.get("addresses", [])}
        self.model = cfg.get("model", DEFAULT_MODEL)
        self.lookback = int(cfg.get("thread_lookback_days", 60))
        self.folders = cfg.get("folders") or ["INBOX"]
        self.delivery = cfg.get("delivery", {})


def _read(path):
    with open(path) as f:
        return f.read().strip()


def get_secret(name, s):
    """Fetch a secret from, in order: systemd credential, 0600 file,
    systemd-creds --user .cred, keyring (imap only)."""
    cred = os.environ.get("CREDENTIALS_DIRECTORY")
    if cred and os.path.exists(os.path.join(cred, name)):
        return _read(os.path.join(cred, name))
    plain = os.path.join(CONFIG_DIR, name)
    if os.path.exists(plain):
        if os.stat(plain).st_mode & 0o077:
            log(f"warning: {plain} is readable by others; chmod 600 it")
        return _read(plain)
    enc = plain + ".cred"
    if os.path.exists(enc):
        try:
            return subprocess.run(["systemd-creds", "--user", "decrypt", "--name", name, enc, "-"],
                                  capture_output=True, check=True).stdout.decode().strip()
        except FileNotFoundError:
            log("warning: systemd-creds not found, cannot decrypt " + enc)
        except subprocess.CalledProcessError as e:
            log(f"warning: cannot decrypt {enc}: {e.stderr.decode(errors='replace').strip()}")
    if name == "imap-password":
        try:
            return subprocess.check_output(
                ["secret-tool", "lookup", "service", "imap", "host", s.host, "user", s.user],
                stderr=subprocess.DEVNULL).decode().strip() or None
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    return None


def connect(s):
    pw = get_secret("imap-password", s)
    if not pw:
        sys.exit("No IMAP password found. See README, section 'Credentials'.")
    delay = 5
    for attempt in range(5):             # network may still be warming up after resume
        try:
            M = imaplib.IMAP4_SSL(s.host, s.port, timeout=30)
            break
        except OSError as e:
            if attempt == 4:
                raise
            log(f"IMAP connect failed ({e}); retrying in {delay}s")
            time.sleep(delay)
            delay *= 2
    # Never retry a rejected login: repeated auth failures trip fail2ban.
    M.login(s.user, pw)
    return M


# ---------- delivery ----------
def md_to_html(md):
    def inline(t):
        t = html.escape(t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        t = re.sub(r"\*(?!\s)(.+?)\*", r"<em>\1</em>", t)
        return t
    out, inlist = ['<div style="font-family:system-ui,sans-serif;max-width:720px;line-height:1.5">'], False
    for line in md.splitlines():
        if line.startswith("- "):
            if not inlist:
                out.append("<ul>")
                inlist = True
            out.append(f"<li>{inline(line[2:])}</li>")
            continue
        if inlist:
            out.append("</ul>")
            inlist = False
        if line.startswith("### "):
            out.append(f"<h3 style='margin:1em 0 .1em'>{inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2 style='border-bottom:1px solid #ddd;padding-bottom:.2em'>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{inline(line[2:])}</h1>")
        elif line.strip():
            out.append(f"<p style='margin:.2em 0'>{inline(line)}</p>")
    if inlist:
        out.append("</ul>")
    out.append("</div>")
    return "\n".join(out)


def send_email(s, subject, md_text):
    d = s.delivery
    host, port = d.get("smtp_host", s.host), int(d.get("smtp_port", 465))
    mail_to, mail_from = d.get("mail_to"), d.get("mail_from", s.user)
    if not mail_to:
        sys.exit("config [delivery].mail_to is required for --send")
    # No fallback to the IMAP password: if they differ, a guaranteed 535 feeds fail2ban.
    pw = get_secret("smtp-password", s)
    if not pw:
        sys.exit("No SMTP password found. See README, section 'Credentials'.")
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, mail_from, mail_to
    msg.set_content(md_text)
    msg.add_alternative(md_to_html(md_text), subtype="html")
    try:
        with smtplib.SMTP_SSL(host, port, timeout=60) as smtp:
            smtp.login(d.get("smtp_user", s.user), pw)
            smtp.send_message(msg)
    except (OSError, smtplib.SMTPException) as e:
        raise DeliveryError(f"SMTP delivery to {mail_to} failed: {e}") from e


# ---------- text helpers ----------
def dstr(s):
    try:
        s = str(make_header(decode_header(s))) if s else ""
    except Exception:
        s = str(s or "")
    s = "".join(ch if (ch.isprintable() or ch == " ") else " " for ch in s)
    return re.sub(r"\s+", " ", s).strip()


def clean_subject(s):
    return RE_PREFIX.sub("", s or "").strip() or "(no subject)"


def display_name(frm):
    return dstr(re.sub(r"\s*<[^>]+>\s*", "", frm or "")).strip(' "') or frm


def addrs(values):
    return [a.lower() for _, a in getaddresses([str(v) for v in values]) if a]


def decode_part(part):
    raw = part.get_payload(decode=True) or b""
    try:
        return raw.decode(part.get_content_charset() or "utf-8", "replace")
    except LookupError:                  # unknown charset name, e.g. "unknown-8bit"
        return raw.decode("utf-8", "replace")


HTML_QUOTE = re.compile(r'(?is)<div[^>]+(?:class="gmail_quote|id="divRplyFwdMsg|id="appendonsend'
                        r'|class="moz-cite-prefix)|-----Original Message-----')


def html_to_text(t):
    t = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", t[:300000])
    m = HTML_QUOTE.search(t)
    if m:
        t = t[:m.start()]
    prev = None
    while prev != t:                     # drop blockquotes, innermost first
        prev = t
        t = re.sub(r"(?is)<blockquote\b(?:(?!<blockquote\b).)*?</blockquote>", " ", t)
    t = re.sub(r"(?i)<br\s*/?>|</(?:p|div|li|tr|h[1-6])>", "\n", t)
    t = html.unescape(re.sub(r"(?s)<[^>]+>", " ", t))
    return "\n".join(re.sub(r"[ \t\xa0]+", " ", ln).strip() for ln in t.splitlines())


# Reply attributions ("On … wrote:", "El … escribió:", …), possibly wrapped over two lines.
ATTRIB_END = re.compile(r"(wrote|escribió|escribiu|escreveu|a écrit|schrieb|scrisse)\s*:\s*$", re.I)
ATTRIB_START = re.compile(r"^(On|El|O|A|Em|Le|Am|Il)\s", re.I)
ORIGINAL_SEP = re.compile(r"^-{2,}\s*(Original Message|Mensaje original|Mensagem original|"
                          r"Message d'origine|Ursprüngliche Nachricht)\s*-{2,}$", re.I)
HDR_FROM = re.compile(r"^(From|De|Von):\s", re.I)
HDR_NEXT = re.compile(r"^(Sent|Date|Enviado|Fecha|Data|Gesendet|Envoyé|To|Para|Subject|Asunto):", re.I)


def clean_text(text):
    lines = text.splitlines()
    out = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith(">"):
            continue
        if ORIGINAL_SEP.match(s) or s.startswith("________") or ln.rstrip() == "--":
            break
        # Outlook-style unquoted history: "From: …" followed by "Sent:/Date:/To:" lines.
        if HDR_FROM.match(s) and any(x.strip() for x in out) \
           and any(HDR_NEXT.match(x.strip()) for x in lines[i + 1:i + 4]) \
           and not any("forward" in x.lower() or "reenviad" in x.lower() for x in lines[max(0, i - 2):i]):
            break
        if len(s) < 300 and ATTRIB_END.search(s):
            if out and ATTRIB_START.match(out[-1].strip()) and not ATTRIB_END.search(out[-1]):
                out.pop()
            continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_body(msg):
    plain = htmlp = None
    for part in msg.walk():
        if part.is_multipart() or "attachment" in str(part.get("Content-Disposition", "")).lower():
            continue
        ct = part.get_content_type()
        if ct == "text/plain" and plain is None:
            plain = decode_part(part)
        elif ct == "text/html" and htmlp is None:
            htmlp = decode_part(part)
    text = clean_text(plain) if plain else ""
    if not text and htmlp:
        text = clean_text(html_to_text(htmlp))
    return text[:MAXCHARS]


# ---------- ollama ----------
def ollama(model, prompt, schema, num_predict=320):
    data = json.dumps({"model": model,
                       "messages": [{"role": "user", "content": prompt}],
                       "stream": False, "think": False, "format": schema,
                       "options": {"temperature": 0.2, "num_predict": num_predict}}).encode()
    req = urllib.request.Request(OLLAMA, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=600) as r:
        content = json.load(r)["message"]["content"].strip()
    try:
        out = json.loads(content)
    except json.JSONDecodeError:
        out = {"category": "FYI", "summary": content}
    if out.get("category") not in CATS:
        out["category"] = "FYI"
    for k in ("summary", "recap"):
        if k in out:
            out[k] = " ".join(str(out[k]).split())
    return out


def delivery_hint(r):
    bits = []
    if r["from_me"]:
        bits.append("sent by the reader")
    elif r["to_me"]:
        bits.append("sent directly to the reader")
    elif r["cc_me"]:
        bits.append("reader in Cc")
    if r["list"]:
        bits.append(f"via mailing list {r['list']}")
    if r["auto"]:
        bits.append("automated sender")
    return ", ".join(bits) or "reader not addressed directly"


def render_msg(r, maxc):
    who = "the reader" if r["from_me"] else r["from"]
    when = r["date"].astimezone().strftime("%Y-%m-%d %H:%M") if r["date"] else "?"
    return (f"From: {who}\nDate: {when}\nDelivery: {delivery_hint(r)}\n"
            f"Subject: {r['subject']}\n\n{(r['body'] or '')[:maxc] or '(no text content)'}")


def fit(msgs, budget):
    """Render messages within a char budget, keeping the newest when it runs out."""
    if not msgs:
        return ""
    per = max(600, min(MAXCHARS, budget // len(msgs)))
    parts, used = [], 0
    for n, m in enumerate(reversed(msgs)):
        block = render_msg(m, per)
        if parts and used + len(block) > budget:
            parts.append(f"[{len(msgs) - n} earlier message(s) omitted]")
            break
        parts.append(block)
        used += len(block)
    return "\n\n----\n".join(reversed(parts))


def triage_intro(s):
    role = f", {s.role}" if s.role else ""
    return (f"You triage work email for {s.name}{role} (\"the reader\"). Categories:\n"
            f"- ACTION: the reader personally needs to reply, review, decide or do something "
            f"(direct questions or requests, deadlines that apply to them).\n"
            f"- FYI: worth knowing, but no personal action.\n"
            f"- AUTO: automated notifications (CI, GitLab, bots, calendars, service newsletters).\n"
            f"- JUNK: spam, cold sales/marketing, unsolicited offers.\n"
            f"Write plain, direct English. Lead with the point, name people and deadlines, and "
            f"call the reader \"you\". Never write phrases like \"no action is required\" (the "
            f"category says that) and don't start with \"This email\" or \"This thread\".\n\n")


def summarize_thread(s, day, new_msgs, prior, ongoing):
    cat = {"type": "string", "enum": list(CATS)}
    if not ongoing:
        schema = {"type": "object", "properties": {"category": cat, "summary": {"type": "string"}},
                  "required": ["category", "summary"]}
        p = (triage_intro(s) + f"A NEW thread started on {day} ({len(new_msgs)} message(s)). "
             "Give its category and a 1-2 sentence summary of the whole thread, including "
             "anything you must do.\n\n" + fit(new_msgs, NEW_BUDGET))
    elif prior:
        schema = {"type": "object", "properties": {"category": cat, "recap": {"type": "string"},
                                                   "summary": {"type": "string"}},
                  "required": ["category", "recap", "summary"]}
        p = (triage_intro(s) + f"An ongoing thread got {len(new_msgs)} new message(s) on {day}. "
             "Give: category (of the new messages); recap = one sentence on what the thread was "
             f"about before {day}; summary = 1-2 sentences on what the new messages add or change "
             "(who said what, if it matters).\n\n"
             f"=== EARLIER MESSAGES ===\n{fit(prior, PRIOR_BUDGET)}\n\n"
             f"=== NEW ON {day} ===\n{fit(new_msgs, NEW_BUDGET)}")
    else:
        schema = {"type": "object", "properties": {"category": cat, "summary": {"type": "string"}},
                  "required": ["category", "summary"]}
        p = (triage_intro(s) + f"These {len(new_msgs)} message(s) from {day} reply to an older "
             "conversation whose earlier messages are not available. Give the category and a 1-2 "
             "sentence summary of what they say.\n\n" + fit(new_msgs, NEW_BUDGET))
    return ollama(s.model, p, schema)


# ---------- IMAP fetch ----------
def parse_internaldate(meta):
    m = IDATE.search(meta)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1).decode(), "%d-%b-%Y %H:%M:%S %z")
    except ValueError:
        return None


def iter_fetch(data):
    """Yield (metadata, payload) per message. Metadata includes items the server sent
    after the literal (e.g. a trailing UID)."""
    for i, item in enumerate(data):
        if isinstance(item, tuple):
            meta = item[0]
            if i + 1 < len(data) and isinstance(data[i + 1], bytes):
                meta += b" " + data[i + 1]
            yield meta, item[1]


def refs_of(h):
    ids, seen = [], set()
    for name in ("References", "In-Reply-To"):
        for i in MIDRE.findall(h.get(name) or ""):
            if i not in seen:
                seen.add(i)
                ids.append(i)
    return ids


def is_spam(h):
    if str(h.get("X-Spam-Flag", "")).strip().lower() == "yes":
        return True
    return any(str(h.get(k, "")).strip().lower().startswith("yes") for k in ("X-Spam-Status", "X-Spam-Report"))


def is_auto(h):
    auto = str(h.get("Auto-Submitted", "")).strip().lower()
    prec = str(h.get("Precedence", "")).strip().lower()
    return (auto not in ("", "no")) or prec in ("bulk", "junk", "auto_reply")


def make_record(folder, meta, payload, me):
    h = email.message_from_bytes(payload)
    um = UIDRE.search(meta)
    irt = MIDRE.findall(h.get("In-Reply-To") or "")
    frm = h.get("From") or ""
    return {
        "mid": (h.get("Message-ID") or "").strip(),
        "folder": folder, "uid": um.group(1) if um else None,
        "from": dstr(frm),
        "subject": dstr(h.get("Subject")) or "(no subject)",
        "date": parse_internaldate(meta),
        "refs": refs_of(h),
        "irt": irt[0] if irt else None,
        "from_me": parseaddr(str(frm))[1].lower() in me,
        "to_me": bool(me & set(addrs(h.get_all("To", [])))),
        "cc_me": bool(me & set(addrs(h.get_all("Cc", [])))),
        "list": dstr(h.get("List-Id")),
        "auto": is_auto(h),
        "spam": is_spam(h),
        "body": None,
    }


def fetch_headers(M, folder, since_s, before_s, me):
    typ, _ = M.select(f'"{folder}"', readonly=True)
    if typ != "OK":
        log(f"!! cannot open folder {folder}")
        return []
    typ, data = M.uid("SEARCH", None, "SINCE", since_s, "BEFORE", before_s)
    uids = data[0].split() if data and data[0] else []
    if not uids:
        return []
    typ, data = M.uid("FETCH", b",".join(uids),
                      f"(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS ({HDR_FIELDS})])")
    return [make_record(folder, meta, payload, me) for meta, payload in iter_fetch(data)]


def fetch_bodies(M, need):
    bodies = {}
    for folder, uids in need.items():
        typ, _ = M.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            continue
        typ, data = M.uid("FETCH", b",".join(sorted(uids)), "(UID BODY.PEEK[])")
        for meta, payload in iter_fetch(data):
            um = UIDRE.search(meta)
            if um:
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
    dedup = {}
    for i, r in enumerate(recs):
        dedup.setdefault(r["mid"] or f"__nomid_{i}", r)
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


def is_ongoing(prior, new_msgs, mids_present):
    # References can hold synthetic IDs (GitLab), so only a missing direct parent counts.
    return bool(prior) or any(r["irt"] and r["irt"] not in mids_present for r in new_msgs)


def local_day(r):
    return r["date"].astimezone().date() if r["date"] else None


# ---------- digest ----------
def collect(s, anchor):
    """Fetch headers and needed bodies for one digest day. Returns (plans, spam_skipped)."""
    since = anchor - timedelta(days=s.lookback - 1)
    before = anchor + timedelta(days=2)      # server dates may differ from local; filter below
    since_s, before_s = since.strftime("%d-%b-%Y"), before.strftime("%d-%b-%Y")
    M = connect(s)
    try:
        recs = []
        for folder in s.folders:
            got = fetch_headers(M, folder, since_s, before_s, s.me)
            recs += got
            log(f"scanned {folder}: {len(got)} msgs ({s.lookback}d)")
        recs = [r for r in recs if local_day(r) is None or local_day(r) <= anchor]
        spam = sum(1 for r in recs if r["spam"] and local_day(r) == anchor)
        recs = [r for r in recs if not r["spam"]]

        threads, mids_present = build_threads(recs)
        plans, need = [], {}
        for t in threads:
            new_msgs = sorted([r for r in t if local_day(r) == anchor], key=lambda r: r["date"])
            if not any(not r["from_me"] for r in new_msgs):
                continue
            prior = sorted([r for r in t if local_day(r) != anchor and r["date"]],
                           key=lambda r: r["date"])[-RECAP_PRIOR_MAX:]
            ongoing = is_ongoing(prior, new_msgs, mids_present)
            for r in new_msgs + prior:
                if r["uid"]:
                    need.setdefault(r["folder"], set()).add(r["uid"])
            plans.append((t, new_msgs, prior, ongoing))
        log(f"{len(plans)} active thread(s) on {anchor}")

        bodies = fetch_bodies(M, need)
        for t, _, _, _ in plans:
            for r in t:
                r["body"] = bodies.get((r["folder"], r["uid"]), "")
        return plans, spam
    finally:
        try:
            M.logout()
        except Exception:
            pass


def digest_day(s, anchor, notes=()):
    plans, spam = collect(s, anchor)
    day = anchor.isoformat()
    results = []
    for t, new_msgs, prior, ongoing in plans:
        ordered = sorted(t, key=lambda r: r["date"] or datetime.max.astimezone())
        subject = clean_subject(ordered[0]["subject"])
        names = []
        for r in ordered:
            nm = "you" if r["from_me"] else display_name(r["from"])
            if nm not in names:
                names.append(nm)
        log(f"summarizing: {subject[:60]}")
        try:
            out = summarize_thread(s, day, new_msgs, prior, ongoing)
        except Exception as e:           # one bad thread must not sink the digest
            log(f"summary failed for {subject[:60]}: {e}")
            out = {"category": "FYI", "summary": f"(summary unavailable: {e})"}
        results.append({
            "cat": out["category"], "subject": subject, "ongoing": ongoing,
            "recap": out.get("recap") if prior else None,
            "summary": out.get("summary") or "(no summary)",
            "participants": ", ".join(names[:4]) + (" …" if len(names) > 4 else ""),
            "folders": ", ".join(sorted({r["folder"] for r in new_msgs})),
            "n_new": len(new_msgs), "sender": display_name(new_msgs[0]["from"]),
            "latest": max(r["date"] for r in new_msgs),
        })
    label = anchor.strftime("%A %Y-%m-%d")
    n_action = sum(r["cat"] == "ACTION" for r in results)
    subject = f"Work digest — {label} ({len(results)} threads" + \
              (f", {n_action} need action)" if n_action else ")")
    return subject, format_digest(results, label, spam, notes)


GROUPS = [("ACTION", "Needs action / reply", "need action"),
          ("FYI", "FYI", "FYI"),
          ("AUTO", "Automated / low priority", "automated"),
          ("JUNK", "Probably junk", "probably junk")]


def format_digest(results, day, spam_skipped=0, notes=()):
    out = [f"# Work email digest — {day}", ""]
    out += [f"*{n}*" for n in notes]
    if not results:
        out.append("No new mail in the selected folders.")
    else:
        counts = [f"{sum(r['cat'] == c for r in results)} {short}" for c, _, short in GROUPS
                  if any(r["cat"] == c for r in results)]
        out += ["**" + " · ".join(counts) + "**", ""]
    for cat, header, _ in GROUPS:
        g = sorted([r for r in results if r["cat"] == cat], key=lambda r: r["latest"], reverse=True)
        if not g:
            continue
        out += [f"## {header} ({len(g)})", ""]
        if cat == "JUNK":
            out += [f"- {r['subject']} — {r['sender']}" for r in g]
            out.append("")
            continue
        for r in g:
            meta = [r["participants"], r["folders"]]
            if r["ongoing"]:
                meta.append(f"{r['n_new']} new")
            elif r["n_new"] > 1:
                meta.append(f"{r['n_new']} messages")
            out += [f"### {r['subject']}", "*" + " · ".join(meta) + "*"]
            if not r["ongoing"]:
                out.append(f"🆕 {r['summary']}")
            else:
                out.append(f"↳ *Earlier:* {r['recap']}" if r["recap"]
                           else "↳ *Reply to an earlier conversation (not in the scanned folders).*")
                out.append(f"**New:** {r['summary']}")
            out.append("")
    if spam_skipped:
        out.append(f"*{spam_skipped} message(s) flagged as spam by the server were skipped.*")
    return "\n".join(out).rstrip() + "\n"


# ---------- catch-up state ----------
def read_state():
    try:
        return date.fromisoformat(_read(STATE_FILE))
    except (OSError, ValueError):
        return None


def write_state(d):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(d.isoformat() + "\n")
    os.replace(tmp, STATE_FILE)


def days_to_send(last, yesterday, cap=CATCHUP_MAX_DAYS):
    """Days after `last` up to `yesterday`, at most `cap` of them. Returns (days, skipped)."""
    start = last + timedelta(days=1) if last else yesterday
    first_allowed = yesterday - timedelta(days=cap - 1)
    skipped = max(0, (first_allowed - start).days)
    start = max(start, first_allowed)
    return [start + timedelta(days=i) for i in range((yesterday - start).days + 1)], skipped


# ---------- main ----------
def list_folders(s):
    M = connect(s)
    try:
        typ, data = M.list()
        for raw in data:
            line = raw.decode(errors="replace")
            m = re.match(r'\([^)]*\)\s+"?[^"]*"?\s+(.+)$', line)
            print(m.group(1).strip().strip('"') if m else line)
    finally:
        M.logout()


def run(a, s):
    if a.list_folders:
        return list_folders(s)
    yesterday = date.today() - timedelta(days=1)
    if a.catch_up:
        last = read_state()
        days, skipped = days_to_send(last, yesterday)
        if not days:
            log(f"nothing to do (last digest sent for {last})")
            return
    else:
        days = [yesterday if a.day == "yesterday" else date.fromisoformat(a.day)]
        skipped = 0
    for i, d in enumerate(days):
        notes = []
        if i == 0 and skipped:
            notes.append(f"{skipped} earlier day(s) were not digested "
                         f"(catch-up covers at most {CATCHUP_MAX_DAYS} days).")
        subject, text = digest_day(s, d, notes)
        if a.send:
            send_email(s, subject, text)
            log(f"sent '{subject}' to {s.delivery.get('mail_to')}")
            if (read_state() or date.min) < d:
                write_state(d)
        else:
            print(text)


def main():
    ap = argparse.ArgumentParser(description="Summarize a day's work email with a local LLM.")
    ap.add_argument("--list-folders", action="store_true")
    ap.add_argument("--folders", default=None, help="comma-separated; overrides config")
    ap.add_argument("--day", default="yesterday", help="'yesterday' or YYYY-MM-DD")
    ap.add_argument("--catch-up", action="store_true",
                    help=f"digest every day since the last one sent (max {CATCHUP_MAX_DAYS})")
    ap.add_argument("--send", action="store_true", help="email the digest instead of printing")
    a = ap.parse_args()

    s = Settings(load_config())
    if a.folders is not None:
        s.folders = [f.strip() for f in a.folders.split(",") if f.strip()]
    try:
        run(a, s)
    except DeliveryError:
        raise
    except Exception:
        if a.send:
            try:
                send_email(s, "Work digest FAILED",
                           "The work email digest failed:\n\n" + traceback.format_exc())
            except Exception as e:
                log(f"could not send failure notice: {e}")
        raise


if __name__ == "__main__":
    main()
