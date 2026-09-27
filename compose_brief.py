"""
compose_brief.py

Turns a pull JSON (from gmail_pull_24h.py) into a dated markdown brief.

For each email, a local model (through Ollama) writes a one-line summary
and picks a bucket: urgent, reply, fyi or promo.

Two checks sit around the model:
  1. Bulk-mail rules. If the model picks "fyi" and the email shows 2 or
     more bulk-mail signs (a noreply/newsletter/marketing sender, a known
     bulk-mail service's domain, or unsubscribe / view-in-browser text),
     the bucket becomes "promo" and the signs are recorded in `via`.
     Urgent and reply are never overridden.
  2. Anomaly bucket. If the model is unreachable, returns something that
     is not valid JSON, or picks a bucket that doesn't exist, the email
     goes to a visible "anomaly" bucket with the reason, instead of being
     guessed.

--dry-run skips the model and uses each source's default bucket.

Stdlib only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import html as _html
import json
import re
import socket
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_OLLAMA = "http://localhost:11434"
DEFAULT_MODEL = "qwen3:30b-a3b-instruct-2507-q4_K_M"
DEFAULT_NUM_CTX = 8192
DEFAULT_TIMEOUT_S = 60
PREVIEW_CHARS = 200

BUCKETS = ("urgent", "reply", "fyi", "promo", "anomaly")

BUCKET_TITLES = {
    "urgent": "Urgent",
    "reply": "Needs reply",
    "fyi": "FYI",
    "promo": "Newsletter / promo",
    "anomaly": "Anomaly (AI classification failure)",
}

LLM_BUCKETS = ("urgent", "reply", "fyi", "promo")

SOURCE_DEFAULT_BUCKET = {
    "outlook_forward": "reply",
    "work_forward": "reply",
    "gmail_primary": "reply",
    "gmail_updates": "fyi",
    "unknown": "promo",
}

SOURCE_ORIGIN_TAG = {
    "work_forward": "Work",
    "outlook_forward": "Outlook",
    "gmail_primary": "Gmail",
    "gmail_updates": "Gmail (Updates)",
    "unknown": "?",
}


# ----- bulk-sender heuristic ------------------------------------------------
# Applied POST-classification. Only overrides "fyi" -> "promo". Never touches
# urgent/reply/anomaly. Threshold 2+ signals to limit false positives.

BULK_LOCAL_RE = re.compile(
    r"^(no-?reply|donotreply|do-not-reply|newsletter|news|marketing|"
    r"promo|promotions|announce|announcements|digest|mailer|campaigns?|"
    r"offers?|deals?|updates?|notify|notifications?|broadcast|bulletin)@",
    re.IGNORECASE,
)

BULK_SUBDOMAIN_RE = re.compile(
    r"@(mail|email|mailer|news|newsletter|announce|marketing|campaign|"
    r"promo|offers|deals|em|e|t|go|notify|list|bounce|send|via|cmp|mktg)\.",
    re.IGNORECASE,
)

BULK_KNOWN_DOMAINS = {
    # ESPs and bulk-mail infrastructure
    "mailchimp.com", "mcsv.net", "mailchimpapp.com",
    "substack.com", "substackcdn.com",
    "convertkit.com", "convertkit-mail2.com", "convertkit-mail.com",
    "beehiiv.com", "beehiiv.net",
    "constantcontact.com", "ccsend.com", "confirmsubscription.com",
    "sendgrid.net", "sendgrid.com",
    "mailgun.org", "mailgun.net",
    "mailerlite.com", "mlsend.com",
    "klaviyomail.com", "kmail-lists.com", "klaviyo.com",
    "list-manage.com", "listmonk.app",
    "mktomail.com", "marketo.com",
    "sailthru.com", "sfmc.co", "exacttarget.com",
    "customeriomail.com", "customer.io",
    "amazonses.com",
    "braze.com",
    "hubspotemail.net", "hs-sendsend.net",
    "pardot.com",
    "activehosted.com", "activecampaign.com",
    "buttondown.email",
    "ghost.io",
    "revue.co",
    "iterable.com",
    "intercom-mail.com", "intercom-mail-forward.com",
}

BULK_BODY_MARKERS = (
    "unsubscribe",
    "view in browser",
    "view this email in your browser",
    "view email in browser",
    "manage preferences",
    "email preferences",
    "update your preferences",
    "you are receiving this email because",
    "this email was sent to",
)


def _extract_addr(from_addr: str) -> str:
    """Return the bare 'user@host' portion of a From header value."""
    if not from_addr:
        return ""
    m = re.search(r"<([^>]+)>", from_addr)
    return (m.group(1) if m else from_addr).strip().lower()


def looks_like_bulk(from_addr: str, body: str) -> tuple[bool, list[str]]:
    """
    Heuristic. Returns (is_bulk, signals).
    2+ signals = bulk. Signals are recorded so the override reason is auditable.
    """
    signals: list[str] = []
    addr = _extract_addr(from_addr)
    if not addr:
        return (False, signals)

    if BULK_LOCAL_RE.search(addr):
        signals.append("bulk_local_part")
    if BULK_SUBDOMAIN_RE.search(addr):
        signals.append("bulk_subdomain")

    domain = addr.split("@", 1)[-1] if "@" in addr else ""
    for d in BULK_KNOWN_DOMAINS:
        if domain == d or domain.endswith("." + d):
            signals.append(f"esp_domain:{d}")
            break

    if body:
        b = body.lower()
        hits = sum(1 for marker in BULK_BODY_MARKERS if marker in b)
        if hits >= 1:
            signals.append(f"body_marker_x{hits}")

    return (len(signals) >= 2, signals)


# ----- data structures ------------------------------------------------------


@dataclass
class Triaged:
    id: str
    from_addr: str
    subject: str
    date: str
    source: str
    summary: str
    bucket: str
    via: str = "llm"  # "llm", "dry_run", "fallback", or "+bulk" suffixed
    error: str = ""


@dataclass
class Brief:
    date: str
    source_file: str
    items: list[Triaged] = field(default_factory=list)
    stats_source_counts: dict[str, int] = field(default_factory=dict)
    fetch_mode: str = ""
    query: str = ""


# ----- io -------------------------------------------------------------------


def load_pull(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "messages" not in data:
        raise ValueError(f"{path} does not look like a pull JSON (missing 'messages')")
    return data


def pick_default_input(inbox_dir: Path) -> Path | None:
    gm = inbox_dir / "gmail_pull.json"
    fx = inbox_dir / "fixture_pull.json"
    if gm.is_file():
        return gm
    if fx.is_file():
        return fx
    return None


# ----- LLM call -------------------------------------------------------------

# Bulk/newsletter/marketing is explicitly assigned to "promo".
# Explicit hierarchy line so the model does not fall back to "fyi" for
# informational-sounding blasts.
PROMPT_TEMPLATE = """\
You are the triage step of a morning-brief pipeline. Classify ONE email.

Return a single JSON object and NOTHING else, using this exact shape:

{{"summary": "<=140 char one-line summary in plain English>",
  "bucket": "urgent" | "reply" | "fyi" | "promo",
  "why": "<short phrase>"}}

Bucket rules:
- "urgent" = action required within 24h from a real person or real system: deadlines, security alerts, blocking requests, time-sensitive approvals.
- "reply" = a real individual is asking you a question or expects a response from you personally. Not urgent. Must not be automated bulk mail.
- "promo" = ANY newsletter, digest, announcement blast, marketing message, promotional offer, or bulk-list mail. Senders like noreply@, newsletter@, marketing@, announce@, updates@, or domains belonging to an ESP (mailchimp, substack, convertkit, beehiiv, sendgrid, mailgun, constantcontact, klaviyo, hubspot, marketo, etc.) ALWAYS belong here, regardless of how informative the content is.
- "fyi" = informational mail addressed to you personally that is NOT promotional: individual order/receipt confirmations, individual account notifications, direct shipping updates, calendar invites, one-off system notices tied to your account.

Hierarchy when unsure:
1. If it looks like bulk, newsletter, digest, or marketing -> "promo".
2. Only pick "fyi" if the mail is clearly directed at you personally and is NOT a mass send.
3. An email with "unsubscribe", "view in browser", or "manage preferences" is almost always "promo".

Email follows. Use headers as context, do not summarize them separately.

From: {from_addr}
To: {to_addr}
Subject: {subject}
Date: {date}
Labels: {labels}

--- body ---
{body}
--- end body ---
"""


def call_ollama(ollama_url: str, model: str, prompt: str,
                num_ctx: int, timeout_s: int) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0.2},
    }
    req = urllib.request.Request(
        ollama_url.rstrip("/") + "/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    obj = json.loads(raw)
    return obj.get("response", "") or ""


def parse_llm_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    m = re.search(r"\{.*?\}", text, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    return obj


def ollama_reachable(ollama_url: str, timeout_s: float = 2.0) -> bool:
    try:
        req = urllib.request.Request(ollama_url.rstrip("/") + "/api/tags")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            resp.read()
        return True
    except (urllib.error.URLError, socket.timeout, ConnectionError):
        return False


# ----- triage ---------------------------------------------------------------


def truncate(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[:n].rstrip() + "..."


def source_bucket(source: str) -> str:
    return SOURCE_DEFAULT_BUCKET.get(source, "promo")


def origin_tag(source: str) -> str:
    return SOURCE_ORIGIN_TAG.get(source, source or "?")


def apply_bulk_override(t: Triaged, body: str) -> Triaged:
    """
    If bucket is 'fyi' and the item looks like bulk mail, switch to 'promo'.
    Records the signals in `via` so the rationale is auditable.
    Never touches urgent/reply/anomaly.
    """
    if t.bucket != "fyi":
        return t
    is_bulk, signals = looks_like_bulk(t.from_addr, body)
    if is_bulk:
        t.bucket = "promo"
        suffix = "+bulk(" + ",".join(signals) + ")"
        t.via = (t.via or "llm") + suffix
    return t


def dry_run_triage(m: dict[str, Any]) -> Triaged:
    body = m.get("body_text") or m.get("snippet") or ""
    t = Triaged(
        id=str(m.get("id", "")),
        from_addr=str(m.get("from", "")),
        subject=str(m.get("subject", "")),
        date=str(m.get("date", "")),
        source=str(m.get("source", "unknown")),
        summary=truncate(re.sub(r"\s+", " ", body).strip(), PREVIEW_CHARS),
        bucket=source_bucket(str(m.get("source", "unknown"))),
        via="dry_run",
    )
    return apply_bulk_override(t, body)


def llm_triage(m: dict[str, Any], ollama_url: str, model: str,
               num_ctx: int, timeout_s: int) -> Triaged:
    body_raw = m.get("body_text") or m.get("snippet") or ""
    prompt = PROMPT_TEMPLATE.format(
        from_addr=m.get("from", ""),
        to_addr=m.get("to", ""),
        subject=m.get("subject", ""),
        date=m.get("date", ""),
        labels=", ".join(m.get("labels") or []),
        body=truncate(body_raw, 3500),
    )
    base = Triaged(
        id=str(m.get("id", "")),
        from_addr=str(m.get("from", "")),
        subject=str(m.get("subject", "")),
        date=str(m.get("date", "")),
        source=str(m.get("source", "unknown")),
        summary="",
        bucket="anomaly",
        via="fallback",
    )
    try:
        text = call_ollama(ollama_url, model, prompt, num_ctx, timeout_s)
    except (urllib.error.URLError, socket.timeout, ConnectionError,
            json.JSONDecodeError, OSError) as e:
        base.summary = truncate(
            re.sub(r"\s+", " ", body_raw).strip(), PREVIEW_CHARS
        )
        base.error = f"ollama call failed: {type(e).__name__}: {e}"
        return base

    obj = parse_llm_json(text)
    if obj is None:
        base.summary = truncate(
            re.sub(r"\s+", " ", body_raw).strip(), PREVIEW_CHARS
        )
        base.error = f"unparsable LLM output: {text[:200]!r}"
        return base

    summary = truncate(str(obj.get("summary", "")).strip(), 200)
    raw_bucket = str(obj.get("bucket", "")).strip().lower()
    if raw_bucket not in LLM_BUCKETS:
        base.summary = summary or truncate(
            re.sub(r"\s+", " ", body_raw).strip(), PREVIEW_CHARS
        )
        base.error = f"LLM returned non-allowed bucket: {raw_bucket!r}"
        return base

    base.summary = summary or truncate(
        re.sub(r"\s+", " ", body_raw).strip(), PREVIEW_CHARS
    )
    base.bucket = raw_bucket
    base.via = "llm"
    return apply_bulk_override(base, body_raw)


# ----- rendering ------------------------------------------------------------


def render_brief(brief: Brief) -> str:
    """Per-item raw-HTML cards, consumed by build_brief_dashboard.py CSS."""
    lines: list[str] = []
    lines.append(f"# Morning brief - {brief.date}")
    lines.append("")
    lines.append(f"Source: `{brief.source_file}`  ")
    lines.append(f"Fetch mode: `{brief.fetch_mode}`  ")
    if brief.query:
        lines.append(f"Query: `{brief.query}`")
    lines.append("")

    by_bucket: dict[str, list[Triaged]] = {b: [] for b in BUCKETS}
    errors: list[Triaged] = []
    for t in brief.items:
        by_bucket.setdefault(t.bucket, []).append(t)
        if t.error:
            errors.append(t)

    for b in BUCKETS:
        lines.append(f"## {BUCKET_TITLES[b]}")
        lines.append("")
        items = by_bucket.get(b, [])
        if not items:
            lines.append("_(none)_")
            lines.append("")
            continue
        anomaly_cls = " item-anomaly" if b == "anomaly" else ""
        for t in items:
            subj = _html.escape(t.subject or "(no subject)")
            sender = _html.escape(t.from_addr or "(unknown)")
            summ = _html.escape(t.summary or "(no summary)")
            tag = _html.escape(origin_tag(t.source))
            lines.append(f'<div class="item{anomaly_cls}">')
            lines.append(f'  <div class="item-subject">{subj}</div>')
            lines.append(f'  <div class="item-summary">{summ}</div>')
            lines.append(f'  <div class="item-meta">from {sender} &middot; {tag}</div>')
            if b == "anomaly" and t.error:
                err = _html.escape(t.error)
                lines.append(f'  <div class="item-reason">reason: {err}</div>')
            lines.append('</div>')
            lines.append('')
        lines.append("")

    lines.append("## Stats")
    lines.append("")
    lines.append(f"- Total messages: {len(brief.items)}")
    if brief.stats_source_counts:
        parts = [f"{k}={v}" for k, v in sorted(brief.stats_source_counts.items())]
        lines.append(f"- By source: {', '.join(parts)}")
    bucket_counts = {b: len(by_bucket.get(b, [])) for b in BUCKETS}
    lines.append(
        "- By bucket: "
        + ", ".join(f"{BUCKET_TITLES[b]}={bucket_counts[b]}" for b in BUCKETS)
    )
    llm_used = sum(1 for t in brief.items if t.via.startswith("llm"))
    dry = sum(1 for t in brief.items if t.via.startswith("dry_run"))
    fb = sum(1 for t in brief.items if t.via == "fallback")
    bulk = sum(1 for t in brief.items if "+bulk" in t.via)
    lines.append(
        f"- Triage via: llm={llm_used}, dry_run={dry}, fallback={fb}, bulk_override={bulk}"
    )
    lines.append("")

    if errors:
        lines.append("## Errors")
        lines.append("")
        for t in errors:
            lines.append(f"- `{t.id}` {t.subject!r}: {t.error}")
        lines.append("")

    return "\n".join(lines)


# ----- orchestration --------------------------------------------------------


def compose(in_path: Path, out_path: Path, date_str: str,
            dry_run: bool, ollama_url: str, model: str,
            num_ctx: int, timeout_s: int) -> Brief:
    pull = load_pull(in_path)
    messages = pull.get("messages") or []

    brief = Brief(
        date=date_str,
        source_file=str(in_path),
        fetch_mode=str(pull.get("fetch_mode", "")),
        query=str(pull.get("query", "")),
    )
    brief.stats_source_counts = {}
    for m in messages:
        src = str(m.get("source", "unknown"))
        brief.stats_source_counts[src] = brief.stats_source_counts.get(src, 0) + 1

    for m in messages:
        if dry_run:
            brief.items.append(dry_run_triage(m))
        else:
            brief.items.append(
                llm_triage(m, ollama_url, model, num_ctx, timeout_s)
            )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(render_brief(brief), encoding="utf-8")
    return brief


# ----- cli ------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Compose morning brief from a pull JSON.")
    p.add_argument("--in", dest="in_path", type=Path, default=None,
                   help="pull JSON (default: newest of gmail_pull.json/fixture_pull.json in --inbox)")
    p.add_argument("--inbox", type=Path, default=None,
                   help="inbox dir to pick default input from")
    p.add_argument("--out", type=Path, required=True, help="output .md path")
    p.add_argument("--date", required=True, help="date stamp for the brief header (YYYY-MM-DD)")
    p.add_argument("--dry-run", action="store_true",
                   help="skip LLM; use body preview + source-default bucket")
    p.add_argument("--ollama", default=DEFAULT_OLLAMA, help="Ollama base URL")
    p.add_argument("--model", default=DEFAULT_MODEL, help="Ollama model tag")
    p.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX, help="Ollama num_ctx")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                   help="per-message LLM timeout seconds")
    return p.parse_args(argv)


def main(argv: list[str]) -> int:
    try:
        args = parse_args(argv)

        in_path = args.in_path
        if in_path is None:
            inbox = args.inbox
            if inbox is None:
                print("[FAIL] --in or --inbox required", file=sys.stderr)
                return 2
            in_path = pick_default_input(inbox)
            if in_path is None:
                print(f"[FAIL] no pull JSON found under {inbox}", file=sys.stderr)
                return 2
        if not in_path.is_file():
            print(f"[FAIL] input not found: {in_path}", file=sys.stderr)
            return 2

        if not args.dry_run:
            if not ollama_reachable(args.ollama):
                print(f"[FAIL] Ollama unreachable at {args.ollama}", file=sys.stderr)
                return 3

        brief = compose(
            in_path=in_path,
            out_path=args.out,
            date_str=args.date,
            dry_run=args.dry_run,
            ollama_url=args.ollama,
            model=args.model,
            num_ctx=args.num_ctx,
            timeout_s=args.timeout,
        )
    except FileNotFoundError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2
    except PermissionError as e:
        print(f"[FAIL] cannot write output: {e}", file=sys.stderr)
        return 4
    except json.JSONDecodeError as e:
        print(f"[FAIL] input is not valid JSON: {e}", file=sys.stderr)
        return 5
    except ValueError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 5
    except Exception as e:
        print(f"[FAIL] unexpected: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    llm_n = sum(1 for t in brief.items if t.via.startswith("llm"))
    dry_n = sum(1 for t in brief.items if t.via.startswith("dry_run"))
    fb_n = sum(1 for t in brief.items if t.via == "fallback")
    bulk_n = sum(1 for t in brief.items if "+bulk" in t.via)
    print(f"[OK] wrote {len(brief.items)} items to {args.out}")
    print(f"[OK] via: llm={llm_n} dry_run={dry_n} fallback={fb_n} bulk_override={bulk_n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
