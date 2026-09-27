"""
gmail_pull_24h.py

Pulls the last 24 hours of mail from Gmail over IMAP and writes it as a
normalized JSON file for compose_brief.py.

Each message gets a `source` tag from its Gmail labels, so the brief can
show where it came from:
  - label "UW"           -> gmail_uw_forward (mail forwarded from a university account)
  - label "from-outlook" -> outlook_forward  (mail forwarded from Outlook)
  - category/updates     -> gmail_updates
  - category/primary     -> gmail_primary

Modes:
  --fixture PATH         read a canned JSON, normalize, write.
  --gmail --config PATH  connect to Gmail via IMAP using credentials in
                         PATH, run Gmail search via X-GM-RAW, pull
                         matching messages, normalize, write.

Credentials INI shape (written by setup_gmail_creds.py):

    [gmail]
    email = you@gmail.com
    app_password = XXXXXXXXXXXXXXXX
    imap_host = imap.gmail.com
    imap_port = 993
    query = (category:primary OR category:updates OR label:UW OR label:from-outlook) newer_than:1d
    mailbox = [Gmail]/All Mail
    max_messages = 500

Stdlib only. Target: Python 3.10+.
"""

from __future__ import annotations

import argparse
import configparser
import email
import email.message
import imaplib
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from email.header import decode_header, make_header
from pathlib import Path
from typing import Any


BODY_TRUNCATE_CHARS = 4096
SCHEMA_VERSION = 1
DEFAULT_QUERY = (
    "(category:primary OR category:updates OR label:UW OR label:from-outlook) "
    "newer_than:1d"
)
DEFAULT_MAILBOX = "[Gmail]/All Mail"
DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
DEFAULT_MAX_MESSAGES = 500


@dataclass
class Message:
    id: str
    thread_id: str
    from_addr: str
    to_addr: str
    subject: str
    date: str              # from the mail header, not "now"
    labels: list[str] = field(default_factory=list)
    snippet: str = ""
    body_text: str = ""
    body_truncated: bool = False
    source: str = "unknown"


@dataclass
class Pull:
    schema_version: int
    fetch_mode: str
    query: str
    messages: list[Message]
    errors: list[str] = field(default_factory=list)


# ----- normalization helpers ------------------------------------------------


def classify_source(labels: list[str]) -> str:
    """Classify a message by its Gmail label set.

    Order matters. The forwarding labels take precedence over category/inbox
    because a forwarded message will typically also be in category:primary.
    We want forwarded mail tagged as such so the composer can show its
    origin per item.
    """
    normalized = {l.lower() for l in labels}
    if "uw" in normalized:
        return "gmail_uw_forward"
    if "from-outlook" in normalized:
        return "outlook_forward"
    if "category/updates" in normalized or "category_updates" in normalized:
        return "gmail_updates"
    if (
        "category/primary" in normalized
        or "category_primary" in normalized
        or "inbox" in normalized
        or "\\inbox" in normalized
    ):
        return "gmail_primary"
    return "unknown"


def truncate_body(text: str) -> tuple[str, bool]:
    if len(text) <= BODY_TRUNCATE_CHARS:
        return text, False
    return text[:BODY_TRUNCATE_CHARS] + "\n[...TRUNCATED...]", True


def build_snippet(body: str) -> str:
    flat = re.sub(r"\s+", " ", body or "").strip()
    return flat[:200]


def normalize_source_dict(src: dict[str, Any]) -> Message:
    body_raw = src.get("body_text", "") or ""
    body, truncated = truncate_body(body_raw)
    labels = list(src.get("labels") or [])
    return Message(
        id=str(src.get("id", "")),
        thread_id=str(src.get("thread_id", "")),
        from_addr=str(src.get("from", "")),
        to_addr=str(src.get("to", "")),
        subject=str(src.get("subject", "")),
        date=str(src.get("date", "")),
        labels=labels,
        snippet=src.get("snippet") or build_snippet(body_raw),
        body_text=body,
        body_truncated=truncated,
        source=src.get("source") or classify_source(labels),
    )


# ----- fixture mode ---------------------------------------------------------


def load_fixture(fixture_path: Path) -> list[dict[str, Any]]:
    if not fixture_path.is_file():
        raise FileNotFoundError(f"fixture not found: {fixture_path}")
    data = json.loads(fixture_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "messages" in data:
        return list(data["messages"])
    if isinstance(data, list):
        return list(data)
    raise ValueError(
        f"fixture at {fixture_path} must be a list or an object with a 'messages' key"
    )


def run_fixture(fixture_path: Path, out_path: Path, query: str) -> Pull:
    raw = load_fixture(fixture_path)
    messages = [normalize_source_dict(m) for m in raw]
    pull = Pull(
        schema_version=SCHEMA_VERSION,
        fetch_mode="fixture",
        query=query,
        messages=messages,
    )
    write_pull(pull, out_path)
    return pull


# ----- gmail mode -----------------------------------------------------------


def decode_hdr(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return str(raw)


def strip_html(html: str) -> str:
    text = re.sub(r"<script[^>]*>.*?</script>", " ", html, flags=re.DOTALL | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def extract_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            cdisp = str(part.get("Content-Disposition") or "")
            if ctype == "text/plain" and "attachment" not in cdisp.lower():
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except Exception:
                    return payload.decode("utf-8", errors="replace")
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                return strip_html(payload.decode(charset, errors="replace"))
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    text = payload.decode(charset, errors="replace")
    if msg.get_content_type() == "text/html":
        text = strip_html(text)
    return text


def parse_envelope(envelope_bytes: bytes) -> tuple[str, list[str]]:
    env = envelope_bytes.decode("utf-8", errors="replace")
    thr_m = re.search(r"X-GM-THRID\s+(\d+)", env)
    thread_id = thr_m.group(1) if thr_m else ""
    lab_m = re.search(r"X-GM-LABELS\s+\(([^)]*)\)", env)
    labels: list[str] = []
    if lab_m:
        raw_labels = lab_m.group(1)
        labels = [q or u for q, u in re.findall(r'"([^"]+)"|(\S+)', raw_labels)]
    norm = []
    for l in labels:
        s = l.lstrip("\\").lower()
        if s:
            norm.append(s)
    return thread_id, norm


def load_gmail_config(config_path: Path) -> dict[str, str]:
    if not config_path.is_file():
        raise FileNotFoundError(f"config not found: {config_path}")
    cp = configparser.ConfigParser()
    cp.read(config_path, encoding="utf-8")
    if "gmail" not in cp:
        raise ValueError(f"config missing [gmail] section: {config_path}")
    section = cp["gmail"]
    if "email" not in section or "app_password" not in section:
        raise ValueError(f"config must define email and app_password: {config_path}")
    return {
        "email": section.get("email", "").strip(),
        "app_password": section.get("app_password", "").strip().replace(" ", ""),
        "imap_host": section.get("imap_host", DEFAULT_IMAP_HOST).strip(),
        "imap_port": section.get("imap_port", str(DEFAULT_IMAP_PORT)).strip(),
        "query": section.get("query", DEFAULT_QUERY).strip(),
        "mailbox": section.get("mailbox", DEFAULT_MAILBOX).strip(),
        "max_messages": section.get("max_messages", str(DEFAULT_MAX_MESSAGES)).strip(),
    }


def quote_gmail_raw(q: str) -> str:
    return '"' + q.replace("\\", "\\\\").replace('"', '\\"') + '"'


def gmail_fetch(cfg: dict[str, str], override_query: str | None = None,
                header_only: bool = False) -> list[dict[str, Any]]:
    host = cfg["imap_host"]
    port = int(cfg["imap_port"])
    user = cfg["email"]
    pw = cfg["app_password"]
    mailbox = cfg["mailbox"]
    query = override_query or cfg["query"]
    limit = int(cfg["max_messages"])

    results: list[dict[str, Any]] = []

    M = imaplib.IMAP4_SSL(host, port)
    try:
        M.login(user, pw)
        status, _ = M.select(f'"{mailbox}"', readonly=True)
        if status != "OK":
            status, _ = M.select("INBOX", readonly=True)
            if status != "OK":
                raise RuntimeError(f"cannot select mailbox {mailbox!r} or INBOX")

        status, data = M.uid("SEARCH", "X-GM-RAW", quote_gmail_raw(query))
        if status != "OK":
            raise RuntimeError(f"search failed: status={status} data={data}")

        uids = (data[0] or b"").split()
        if limit and len(uids) > limit:
            uids = uids[-limit:]

        for uid in uids:
            if header_only:
                status, msg_data = M.uid(
                    "FETCH", uid,
                    "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)] X-GM-THRID X-GM-LABELS)",
                )
            else:
                status, msg_data = M.uid(
                    "FETCH", uid, "(RFC822 X-GM-THRID X-GM-LABELS)",
                )
            if status != "OK" or not msg_data:
                continue

            envelope_bytes = b""
            body_bytes = b""
            for item in msg_data:
                if isinstance(item, tuple) and len(item) >= 2:
                    envelope_bytes = item[0] or b""
                    body_bytes = item[1] or b""
                    break

            thread_id, labels = parse_envelope(envelope_bytes)

            try:
                msg = email.message_from_bytes(body_bytes)
            except Exception:
                continue

            from_hdr = decode_hdr(msg.get("From"))
            to_hdr = decode_hdr(msg.get("To"))
            subject = decode_hdr(msg.get("Subject"))
            date_hdr = (msg.get("Date") or "").strip()
            mid = (msg.get("Message-ID") or "").strip().strip("<>")

            if header_only:
                body_text = ""
            else:
                body_text = extract_text_body(msg)

            results.append({
                "id": mid or uid.decode("ascii", errors="replace"),
                "thread_id": thread_id,
                "from": from_hdr,
                "to": to_hdr,
                "subject": subject,
                "date": date_hdr,
                "labels": labels,
                "body_text": body_text,
            })
    finally:
        try:
            M.logout()
        except Exception:
            pass

    return results


def run_gmail(config_path: Path, out_path: Path, query: str | None,
              header_only: bool = False) -> Pull:
    cfg = load_gmail_config(config_path)
    effective_query = query or cfg.get("query") or DEFAULT_QUERY
    raw = gmail_fetch(cfg, override_query=effective_query, header_only=header_only)
    messages = [normalize_source_dict(m) for m in raw]
    pull = Pull(
        schema_version=SCHEMA_VERSION,
        fetch_mode="gmail_headers_only" if header_only else "gmail",
        query=effective_query,
        messages=messages,
    )
    write_pull(pull, out_path)
    return pull


# ----- output ---------------------------------------------------------------


def pull_to_jsonable(pull: Pull) -> dict[str, Any]:
    d = asdict(pull)
    for m in d["messages"]:
        m["from"] = m.pop("from_addr")
        m["to"] = m.pop("to_addr")
    return d


def write_pull(pull: Pull, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(pull_to_jsonable(pull), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ----- cli ------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Pull last 24h of mail, normalized, for morning-triage.",
    )
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", type=Path, help="path to a fixture JSON file")
    mode.add_argument("--gmail", action="store_true", help="use real Gmail IMAP")
    p.add_argument("--config", type=Path, help="credentials file (required with --gmail)")
    p.add_argument("--out", type=Path, required=True, help="output JSON path")
    p.add_argument("--query", default=None, help="override Gmail search query")
    p.add_argument(
        "--header-only", action="store_true",
        help="gmail mode: fetch only headers (for smoke tests)",
    )
    args = p.parse_args(argv)
    if args.gmail and args.config is None:
        p.error("--gmail requires --config")
    return args


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)
        if args.fixture is not None:
            pull = run_fixture(args.fixture, args.out, args.query or DEFAULT_QUERY)
        else:
            pull = run_gmail(args.config, args.out, args.query, header_only=args.header_only)
    except FileNotFoundError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2
    except NotImplementedError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 3
    except json.JSONDecodeError as e:
        print(f"[FAIL] fixture is not valid JSON: {e}", file=sys.stderr)
        return 4
    except imaplib.IMAP4.error as e:
        print(f"[FAIL] IMAP error: {e}", file=sys.stderr)
        return 5
    except ValueError as e:
        print(f"[FAIL] config problem: {e}", file=sys.stderr)
        return 6
    except Exception as e:
        print(f"[FAIL] unexpected error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    print(f"[OK] wrote {len(pull.messages)} messages to {args.out}")
    print(f"[OK] fetch_mode={pull.fetch_mode} query={pull.query!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
