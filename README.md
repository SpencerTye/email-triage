# Email triage agent

This agent runs each morning on a home computer. It pulls the last 24 hours of Gmail, asks a local language model to summarize each email in one line and sort it into a bucket, and writes the results as a dated brief and a web dashboard.

The buckets are urgent, needs reply, FYI, and newsletter/promo. When the model fails on an email, that email goes into a fifth bucket, anomaly, along with the reason.

## Track record

The agent ran every morning from April 14 to May 20, 2026. That is 37 days in a row with no missed day, and every run pulled live Gmail.

- It processed 709 emails. The model classified 708 of them.
- The one failure was on April 29. The model's answer for one email could not be used, so that email went to the anomaly bucket.
- The bulk-mail rules moved 21 emails from FYI to promo.
- 59 emails (8%) were flagged as needing action: 23 urgent and 36 needs reply.
- 471 emails (66%) were newsletters and promotions.

These numbers come from the stats section at the end of each day's brief. The briefs summarize real email, so they are not in this repo.

During those 37 days, a chain of Windows batch files ran the three steps. `run_morning_triage.py` replaces that chain. It runs the same steps in the same order and records the result the same way.

## Scripts

| Script | What it does |
|---|---|
| `gmail_pull_24h.py` | Logs into Gmail over IMAP and saves the last 24 hours of mail as JSON. |
| `compose_brief.py` | Sends each email to a local model through Ollama for a summary and a bucket, applies the bulk-mail rules, and writes the brief as markdown. |
| `build_brief_dashboard.py` | Renders all briefs as one HTML page, newest first. |
| `run_morning_triage.py` | Runs the three scripts above in order. The scheduler starts this one. |
| `setup_gmail_creds.py` | One-time setup. Saves the Gmail app password and checks that login works. |

## Bulk-mail rules

The model often puts newsletters in FYI. After the model answers, a set of rules checks each email for signs of bulk mail:

- a sender such as noreply@, newsletter@ or marketing@
- a domain that belongs to a bulk-mail service, such as Mailchimp, Substack or SendGrid
- unsubscribe or view-in-browser text in the body

If the model chose FYI and the email has two or more of these signs, the email moves to promo, and the signs are recorded next to it. The rules never change an urgent or needs-reply email.

## Anomaly bucket

An email goes to the anomaly bucket when Ollama can't be reached or times out, when the model's reply is not valid JSON, or when the model picks a bucket that doesn't exist. The brief lists each of these emails with its reason.

The first emails of a run can time out while a large model is still loading. They go to the anomaly bucket in the same way.

## Run result

Each run writes `last_run.txt` to the data folder with one of three results:

- PASS: the pull and the brief both worked.
- DEGRADED: the Gmail pull failed, so the brief was built from the last pull on disk.
- FAIL: no brief was written.

## Setup

You need Python 3.10 or newer, Ollama with a model pulled, and a Gmail account with 2-Step Verification turned on. The scripts use only the Python standard library, except the dashboard, which needs the `markdown` package.

```
pip install markdown
ollama pull qwen3:30b-a3b-instruct-2507-q4_K_M
```

1. Copy `settings.example.ini` to `settings.ini`. Set the data folder, the credentials file and the model.
2. Create a Gmail app password at https://myaccount.google.com/apppasswords, then run:
   ```
   python setup_gmail_creds.py --out gmail.ini
   ```
   This saves your Gmail address and app password to `gmail.ini` as plain text. On Windows, it then limits the file so only your user account and SYSTEM can read it. On other systems, add `--skip-acl`. It finishes with a test login.
3. To try it without Gmail, run it on the sample inbox. The sample emails are made up.
   ```
   python run_morning_triage.py --fixture
   python run_morning_triage.py --fixture --dry-run
   ```
   The second command skips the model.
4. Run it for real with `python run_morning_triage.py`, and schedule that command once a day with Windows Task Scheduler or cron.

`settings.ini`, `gmail.ini` and the data folder are listed in `.gitignore`.

## Gmail labels

The pull tags each email with where it came from. Mail with the Gmail label `UW` is tagged as forwarded from a university account, and mail with the label `from-outlook` is tagged as forwarded from Outlook. The setup script asks which Gmail search to use. Its suggested search covers the Primary and Updates tabs and the `from-outlook` label. To include the `UW` label too, add `OR label:UW` at the prompt or in the `query` line of `gmail.ini`.
