# work-sum

Daily digest of yesterday's work email, summarized by a **local** LLM (Ollama).
Mail is read over IMAP in read-only mode (`EXAMINE` + `BODY.PEEK`, flags are never
touched), grouped into threads, summarized, and emailed to you each morning.

- New thread (started on the digest day): one summary of the whole thread.
- Ongoing thread: a short recap of earlier messages plus what the new messages add.
- Threads are grouped as **Needs action**, **FYI** and **Automated**.

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
```

Logs: `journalctl --user -u mail-digest`. Run now: `systemctl --user start mail-digest.service`.
