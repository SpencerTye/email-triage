"""
setup_gmail_creds.py

Interactive helper that:
  1. Prompts for Gmail address and 16-char app password (app password is
     read with getpass so it does not echo to the console).
  2. Writes the credential INI to the path passed with --out.
  3. Restricts the file's NTFS ACL so only the current Windows user (and
     SYSTEM) can read it, via `icacls`.
  4. Runs a live IMAP smoke test: login, SELECT the configured mailbox,
     run the configured Gmail X-GM-RAW search, report the UID count, then
     logout. No message bodies are fetched.

Exit codes:
  0  success (cred file written, ACL set, IMAP smoke test passed)
  2  cannot write cred file
  3  icacls failure
  4  IMAP login failure
  5  IMAP SELECT failure on configured mailbox and INBOX fallback
  6  IMAP SEARCH failure
  7  user aborted at prompt

Stdlib only. Windows-only (relies on icacls for ACL lockdown).
"""

from __future__ import annotations

import argparse
import configparser
import getpass
import imaplib
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAILBOX = "[Gmail]/All Mail"
DEFAULT_QUERY = "(category:primary OR category:updates OR label:from-outlook) newer_than:1d"
DEFAULT_MAX_MESSAGES = "500"


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        raw = input(f"{label}{suffix}: ").strip()
    except EOFError:
        return default
    return raw or default


def prompt_required(label: str) -> str:
    while True:
        v = input(f"{label}: ").strip()
        if v:
            return v
        print("  value is required")


def prompt_password(label: str) -> str:
    while True:
        v = getpass.getpass(f"{label} (input hidden): ").strip().replace(" ", "")
        if not v:
            print("  value is required")
            continue
        if len(v) != 16 or not v.isalnum():
            print(f"  warning: Gmail app passwords are 16 alphanumeric chars (got {len(v)}).")
            confirm = input("  use anyway? [y/N]: ").strip().lower()
            if confirm != "y":
                continue
        return v


def write_cred(path: Path, data: dict[str, str]) -> None:
    cp = configparser.ConfigParser()
    cp["gmail"] = data
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        cp.write(fh)


def lockdown_acl(path: Path) -> None:
    """Restrict file read to the current user and SYSTEM."""
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    user = user.lstrip("\\")
    # Reset inheritance, remove broad Users, grant only current user and SYSTEM.
    cmds = [
        ["icacls", str(path), "/inheritance:r"],
        ["icacls", str(path), "/grant:r", f"{user}:F"],
        ["icacls", str(path), "/grant:r", "SYSTEM:F"],
    ]
    for cmd in cmds:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(
                f"icacls failed ({r.returncode}): {' '.join(cmd)}\n"
                f"stdout: {r.stdout}\nstderr: {r.stderr}"
            )


def _quote_gmail_raw(q: str) -> str:
    """Wrap as IMAP quoted-string for X-GM-RAW."""
    return '"' + q.replace("\\", "\\\\").replace('"', '\\"') + '"'


def imap_smoke_test(cfg: dict[str, str]) -> tuple[int, str]:
    """Log in, select mailbox, run search, report UID count. No body fetches."""
    host = cfg["imap_host"]
    port = int(cfg["imap_port"])
    user = cfg["email"]
    pw = cfg["app_password"]
    mailbox = cfg["mailbox"]
    query = cfg["query"]

    M = imaplib.IMAP4_SSL(host, port)
    try:
        try:
            M.login(user, pw)
        except imaplib.IMAP4.error as e:
            return 4, f"login failed: {e}"

        status, _ = M.select(f'"{mailbox}"', readonly=True)
        used_mailbox = mailbox
        if status != "OK":
            status, _ = M.select("INBOX", readonly=True)
            used_mailbox = "INBOX"
            if status != "OK":
                return 5, f"SELECT failed for {mailbox!r} and INBOX"

        try:
            status, data = M.uid("SEARCH", "X-GM-RAW", _quote_gmail_raw(query))
        except imaplib.IMAP4.error as e:
            return 6, f"SEARCH failed: {e}"
        if status != "OK":
            return 6, f"SEARCH returned status {status}: {data!r}"

        uids = (data[0] or b"").split()
        return 0, f"login OK, mailbox={used_mailbox!r}, matched {len(uids)} UID(s)"
    finally:
        try:
            M.logout()
        except Exception:
            pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interactive Gmail credential setup.")
    p.add_argument("--out", type=Path, required=True, help="path to cred INI to write")
    p.add_argument("--skip-acl", action="store_true",
                   help="skip icacls lockdown (for non-Windows testing only)")
    p.add_argument("--skip-smoke", action="store_true",
                   help="skip IMAP smoke test (do not recommend)")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    print("=" * 70)
    print("Gmail credential setup")
    print("=" * 70)
    print()
    print("You will be prompted for:")
    print("  1. your Gmail address")
    print("  2. a Google app password (16 alphanumeric chars, no spaces)")
    print()
    print("An app password is NOT your normal Google account password. Create one at")
    print("  https://myaccount.google.com/apppasswords")
    print("with 2-Step Verification enabled first at")
    print("  https://myaccount.google.com/signinoptions/two-step-verification")
    print()

    try:
        go = input("Proceed? [Y/n]: ").strip().lower()
    except EOFError:
        go = "n"
    if go == "n":
        print("[ABORT] user declined")
        return 7

    email_addr = prompt_required("Gmail address")
    app_pw = prompt_password("App password")
    imap_host = prompt("IMAP host", DEFAULT_IMAP_HOST)
    imap_port = prompt("IMAP port", str(DEFAULT_IMAP_PORT))
    mailbox = prompt("Mailbox", DEFAULT_MAILBOX)
    query = prompt("Gmail search query", DEFAULT_QUERY)
    max_messages = prompt("Max messages per pull", DEFAULT_MAX_MESSAGES)

    cfg = {
        "email": email_addr,
        "app_password": app_pw,
        "imap_host": imap_host,
        "imap_port": imap_port,
        "mailbox": mailbox,
        "query": query,
        "max_messages": max_messages,
    }

    out_path: Path = args.out
    try:
        write_cred(out_path, cfg)
    except OSError as e:
        print(f"[FAIL] cannot write {out_path}: {e}", file=sys.stderr)
        return 2
    print(f"[OK] wrote cred file: {out_path}")

    if args.skip_acl:
        print("[SKIP] icacls lockdown (--skip-acl)")
    else:
        try:
            lockdown_acl(out_path)
            print(f"[OK] ACL locked down to {os.environ.get('USERNAME','<user>')} + SYSTEM")
        except RuntimeError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            return 3

    if args.skip_smoke:
        print("[SKIP] IMAP smoke test (--skip-smoke)")
        return 0

    print()
    print("Running IMAP smoke test (login + SELECT + SEARCH, no body fetch)...")
    rc, msg = imap_smoke_test(cfg)
    if rc == 0:
        print(f"[OK] {msg}")
        return 0
    print(f"[FAIL] {msg}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
