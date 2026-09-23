# Improvement plan

Findings come from a review of the first version (`bf5c24b`). Some were reproduced
against a real day of mail, and the rest come from reading the code.

## 1. Bugs that lose or garble content (reproduced)

- **Bottom-posted / inline replies are dropped.** `clean_body` stops at the first
  `On … wrote:` line, so a reply written below the quote comes out empty. The model
  then answers "no message body provided".
  - Drop attribution lines (EN/ES/GL/PT/FR/DE, including wrapped ones) and `>` lines
    instead of cutting there.
  - Only cut at separators that mark unquoted history: Outlook
    `-----Original Message-----`, `From:`/`Sent:` blocks, `________`.
  - Never send an empty body to the model.
- **Missed days are never digested.** Each run only looks at "yesterday", so if the
  machine is off at 07:00 the day is lost.
  - Add `--catch-up`, backed by a state file holding the last day sent. It digests
    every missing day, oldest first, and caps the backlog at 7 days.
  - Raise the unit timeout to match.
- **New GitLab issues are shown as "continuation outside the 60-day window".**
  GitLab puts synthetic IDs in `References`. Treat a thread as ongoing only if
  earlier messages were actually found, or if the direct `In-Reply-To` parent is
  missing. Fix the wording: the parent is usually in an unscanned folder, not old.

## 2. Reliability (from reading the code)

- An unknown MIME charset raises `LookupError` and kills the whole run. Decode
  defensively instead.
- `connect()` retries login even on authentication failures. Five bad logins can trip
  fail2ban. Retry only on network errors, with backoff.
- SMTP falls back to the IMAP password, which is guaranteed to fail (535) on servers
  that use separate passwords. Remove the fallback, and report credential decrypt
  errors instead of swallowing them.
- One failed model call aborts the digest. Handle errors per thread.
- A failure means no email and no signal. On error, send a short failure notice,
  except when the failure is SMTP auth itself.
- Parsing the `CATEGORY | text` reply is fragile (markdown bold, multi-line output).
  Use Ollama structured output with a JSON schema instead.

## 3. Classification

- The model doesn't know who the reader is or how the mail arrived.
  - Add an `[identity]` config section (name, addresses, role).
  - Fetch `To`/`Cc`/`List-Id`/`Auto-Submitted`/`Precedence`.
  - Tell the model whether each message was sent directly, as Cc, via a list, or by
    an automated sender.
- Skip messages the server flagged as spam (`X-Spam-Flag`, `X-Spam-Status`,
  `X-Spam-Report`).
- Add a `JUNK` category for unsolicited marketing that got past the filter, and list
  those threads compactly at the bottom.
- Skip threads whose only new messages were sent by the reader.
- `summ_new_thread` truncates the joined thread at 6000 chars, which drops the newest
  messages. Split the budget across messages and prefer the newest.
- Move the IMAP host and user out of the code and into config.

## 4. Readability

- Write one summary per thread covering all of the day's new messages, not one line
  per reply. This gives better context and fewer model calls.
- Stop the "No action is required from X" boilerplate. The category already says that.
- Add a one-line overview at the top ("2 need action · 8 FYI · …"), and show the
  folder and new-message count on each thread.
- `_Recap:_` renders literal underscores in HTML. Use one emphasis syntax that the
  HTML converter understands.
- Minor cleanups:
  - `s == "-- "` can never match after `.strip()`.
  - `max_per_folder` is documented but never implemented.
  - The "(60d)" log label is hardcoded.

## 5. Tests

A stdlib `unittest` suite with synthetic fixtures:

- body cleaning: inline, top-posted, Outlook, Spanish attribution, signature, HTML,
  bad charset
- new-vs-ongoing thread detection
- catch-up day selection
- HTML rendering

## Later (not in this round)

- Link vote-bot notifications to their discussion thread (`[assembly] vote: …` shows
  up as several single-message threads).
- Optional footer with a count of Junk messages from internal senders.
