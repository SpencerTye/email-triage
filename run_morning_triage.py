"""
run_morning_triage.py

One morning run: pull the last 24 hours of Gmail, compose the brief, and
rebuild the dashboard. Meant to be started once a day by a scheduler
(Windows Task Scheduler, cron).

Settings come from settings.ini next to this file (copy
settings.example.ini to start). Each run writes last_run.txt in the data
folder:

    pull=<exit code>
    compose=<exit code>
    result=PASS | DEGRADED | FAIL

Compose is the step that matters. If the pull fails but compose works on
the last pull on disk, the result is DEGRADED. If compose fails, FAIL.

Options:
  --fixture   use fixture_inbox.json instead of Gmail
  --dry-run   skip the model (source-default buckets)
  --settings  path to a settings file

Python 3.10+.
"""

from __future__ import annotations

import argparse
import configparser
import datetime
import sys
from pathlib import Path

import compose_brief
import gmail_pull_24h

HERE = Path(__file__).resolve().parent


def load_settings(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(
            f"settings file not found: {path} (copy settings.example.ini to settings.ini)"
        )
    cp = configparser.ConfigParser()
    cp.read(path, encoding="utf-8")
    return {
        "data_dir": cp.get("paths", "data_dir", fallback="./data"),
        "credentials": cp.get("paths", "credentials", fallback="./gmail.ini"),
        "ollama_url": cp.get("ollama", "url", fallback=compose_brief.DEFAULT_OLLAMA),
        "model": cp.get("ollama", "model", fallback=compose_brief.DEFAULT_MODEL),
        "num_ctx": cp.get(
            "ollama", "num_ctx", fallback=str(compose_brief.DEFAULT_NUM_CTX)
        ),
    }


def resolve(p: str, base: Path) -> Path:
    path = Path(p).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        description="Pull, triage and render one morning brief."
    )
    ap.add_argument("--settings", type=Path, default=HERE / "settings.ini")
    ap.add_argument(
        "--fixture", action="store_true", help="use fixture_inbox.json instead of Gmail"
    )
    ap.add_argument("--dry-run", action="store_true", help="skip the model")
    args = ap.parse_args(argv)

    try:
        s = load_settings(args.settings)
    except FileNotFoundError as e:
        print(f"[FAIL] {e}", file=sys.stderr)
        return 2

    base = args.settings.resolve().parent
    data_dir = resolve(s["data_dir"], base)
    inbox = data_dir / "_inbox"
    briefs = data_dir / "briefs"
    inbox.mkdir(parents=True, exist_ok=True)
    briefs.mkdir(parents=True, exist_ok=True)

    # 1. Pull. A failed pull is not fatal: compose falls back to the last pull on disk.
    if args.fixture:
        pull_rc = gmail_pull_24h.main(
            [
                "--fixture",
                str(HERE / "fixture_inbox.json"),
                "--out",
                str(inbox / "fixture_pull.json"),
            ]
        )
    else:
        pull_rc = gmail_pull_24h.main(
            [
                "--gmail",
                "--config",
                str(resolve(s["credentials"], base)),
                "--out",
                str(inbox / "gmail_pull.json"),
            ]
        )
    print(f"[INFO] pull exit {pull_rc}")

    # 2. Compose.
    date_stamp = datetime.date.today().isoformat()
    compose_args = [
        "--inbox",
        str(inbox),
        "--out",
        str(briefs / f"{date_stamp}.md"),
        "--date",
        date_stamp,
        "--ollama",
        s["ollama_url"],
        "--model",
        s["model"],
        "--num-ctx",
        s["num_ctx"],
    ]
    if args.fixture:
        compose_args[0:2] = ["--in", str(inbox / "fixture_pull.json")]
    if args.dry_run:
        compose_args.append("--dry-run")
    compose_rc = compose_brief.main(compose_args)
    print(f"[INFO] compose exit {compose_rc}")

    # 3. Dashboard. Needs the markdown package; a failure here does not change the result.
    try:
        import build_brief_dashboard

        build_brief_dashboard.main(["--briefs-dir", str(briefs)])
    except SystemExit:
        print("[WARN] dashboard not built (markdown package missing)", file=sys.stderr)

    if compose_rc != 0:
        result = "FAIL"
    elif pull_rc != 0:
        result = "DEGRADED"
    else:
        result = "PASS"
    (data_dir / "last_run.txt").write_text(
        f"pull={pull_rc}\ncompose={compose_rc}\nresult={result}\n", encoding="utf-8"
    )
    print(f"[RESULT] {result}")
    return 0 if result != "FAIL" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
