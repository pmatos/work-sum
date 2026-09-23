# work-sum

Daily digest of yesterday's work email, summarized by a **local** LLM (Ollama).
Mail is read over IMAP in read-only mode (`EXAMINE` + `BODY.PEEK`, flags are never
touched), grouped into threads, summarized, and emailed to you each morning.

- New thread (started on the digest day): one summary of the whole thread.
- Ongoing thread: a short recap of earlier messages plus what the new messages add.
- One summary per thread covers all of the day's new messages.
- Threads are grouped as **Needs action**, **FYI**, **Automated** and **Probably junk**
  (listed compactly). Mail the server flagged as spam is skipped.
- The model is told who you are and whether each message was sent to you directly,
  as Cc, through a mailing list or by an automated sender.
- Missed days (machine off at 07:00) are caught up on the next run, up to 7 days.

Mail content only goes to the local Ollama instance.

## Requirements

- Python ≥ 3.11 (stdlib only)
- [Ollama](https://ollama.com) with a chat model (default `gemma4:26b-a4b-it-q4_K_M`)
- systemd (user instance) for the daily timer; `systemd-creds` for encrypted credentials

## Install

```sh
ln -s "$PWD/mail_digest.py" ~/.local/bin/mail_digest.py
mkdir -p ~/.config/mail-digest
cp config.example.toml ~/.config/mail-digest/config.toml   # then edit it
ln -s "$PWD/systemd/mail-digest.service" ~/.config/systemd/user/
ln -s "$PWD/systemd/mail-digest.timer"   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now mail-digest.timer
loginctl enable-linger "$USER"     # run even when not logged in
```

## Credentials

Passwords are never stored in the config or the repo. The script looks for a
secret named `imap-password` / `smtp-password`, in order:

1. `$CREDENTIALS_DIRECTORY/<name>` (systemd `LoadCredential=`)
2. `~/.config/mail-digest/<name>` — plaintext, must be mode 0600
3. `~/.config/mail-digest/<name>.cred` — encrypted with `systemd-creds --user`
4. the desktop keyring (`secret-tool`, IMAP only)

Recommended (works under a locked keyring, e.g. from a timer):

```sh
cd ~/.config/mail-digest
systemd-ask-password 'IMAP password:' | systemd-creds --user encrypt --name imap-password - imap-password.cred
systemd-ask-password 'SMTP password:' | systemd-creds --user encrypt --name smtp-password - smtp-password.cred
```

Some servers use different IMAP and SMTP passwords. Failed SMTP logins can trip
fail2ban, so test delivery carefully.

## Usage

```sh
mail_digest.py --list-folders          # show IMAP folder names
mail_digest.py --day yesterday         # print digest to stdout
mail_digest.py --day 2026-09-22        # a specific day
mail_digest.py --day yesterday --send  # email it
mail_digest.py --catch-up --send       # email every day since the last one sent (what the timer runs)
```

The last day sent is recorded in `~/.local/state/mail-digest/last-sent`. If a run
fails before sending, it tries to email you a short failure notice, unless SMTP
itself is what failed.

## Configuration

See [`config.example.toml`](config.example.toml): `folders`, `[account]` (IMAP
host and user), `[identity]` (name, role and addresses, used to spot mail addressed
to you) and `[delivery]` (SMTP settings and recipient).

## Tests

```sh
python3 -m unittest discover -s tests
```

Logs: `journalctl --user -u mail-digest`. Run now: `systemctl --user start mail-digest.service`.
