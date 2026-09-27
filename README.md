# Email triage agent

A daily email-triage agent that runs on your own computer. Each morning it pulls the last 24 hours of Gmail, has a local language model sort each email and summarize it in one line, and writes a dated brief plus a web dashboard.

Every email lands in one of four buckets: **urgent**, **needs reply**, **FYI** or **newsletter/promo**. If the model fails on an email, that email goes to a fifth bucket, **anomaly**, with the reason shown. It is never silently guessed.

## Track record

It ran unattended every morning from April 14 to May 20, 2026: 37 days in a row, no missed day, every run a live Gmail pull.

- 709 emails processed. The model classified 708 of them.
- 1 failure, on April 29: the model's answer for one email could not be used, and that email went to the anomaly bucket with the reason.
- 21 emails that the model called FYI were moved to promo by the bulk-mail rules (below).
- 59 emails (8%) were flagged as needing action: 23 urgent, 36 needs reply.
- 471 (66%) were newsletters and promotions.

These numbers come from the stats section at the end of each day's brief. The briefs themselves summarize real email, so they are not in this repo.

## How it works

| Script | What it does |
|---|---|
| `gmail_pull_24h.py` | Logs into Gmail over IMAP and saves the last 24 hours of mail as JSON. |
| `compose_brief.py` | Sends each email to a local model through [Ollama](https://ollama.com) for a summary and a bucket, applies the bulk-mail rules, and writes the day's brief as markdown. |
| `build_brief_dashboard.py` | Renders all briefs as one HTML page, newest first. |
| `run_morning_triage.py` | Runs the three steps above in order. This is what the scheduler starts. |
| `setup_gmail_creds.py` | One-time setup: saves your Gmail app password and checks that login works. |

**Bulk-mail rules.** Models tend to call newsletters "FYI" because they read as informational. After the model answers, a set of rules looks for bulk-mail signs: a noreply/newsletter/marketing sender, a known bulk-mail service's domain (Mailchimp, Substack, SendGrid and others), and unsubscribe or view-in-browser text. If an email the model called FYI has two or more signs, it moves to promo, and the signs are recorded next to it. Urgent and needs-reply are never overridden.

**Anomaly bucket.** An email goes to anomaly when Ollama can't be reached or times out, when the reply isn't valid JSON, or when the model picks a bucket that doesn't exist. The brief lists each one with its reason. The first emails of a run can time out while a large model is still loading into memory. They land in anomaly the same way.

**Run result.** Each run writes `last_run.txt` in the data folder: `PASS`, `DEGRADED` (the Gmail pull failed, so the brief was built from the last pull on disk) or `FAIL` (no brief was written).

## Setup

Requires Python 3.10+, [Ollama](https://ollama.com) with a model pulled, and a Gmail account with 2-Step Verification. The scripts use only the Python standard library, except the dashboard, which needs `markdown`:

```
pip install markdown
ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M
```

1. Copy `settings.example.ini` to `settings.ini` and set the data folder, the credentials file and the model.
2. Create a Gmail app password at https://myaccount.google.com/apppasswords, then run:
   ```
   python setup_gmail_creds.py --out gmail.ini
   ```
   This writes your Gmail address and app password to `gmail.ini` in plain text. On Windows it then restricts the file so only your user account and SYSTEM can read it (`--skip-acl` skips that step on other systems). It finishes with a test login.
3. Try it on the included sample inbox of made-up emails:
   ```
   python run_morning_triage.py --fixture            # with the model
   python run_morning_triage.py --fixture --dry-run  # without the model
   ```
4. Run it for real with `python run_morning_triage.py`, and schedule that command once a day (Windows Task Scheduler or cron).

`settings.ini`, `gmail.ini` and the data folder are in `.gitignore`.

## Gmail labels

The pull tags each email with where it came from. If you forward another account into Gmail, give that mail the label `work` or `from-outlook`, and the brief marks it as such. The default search covers the Primary and Updates tabs plus those two labels. Change it with the `query` line in `gmail.ini`.
