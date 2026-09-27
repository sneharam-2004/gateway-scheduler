"""report.py - mail the outcome of a scheduled run.

WHY THIS LIVES IN THE PUBLIC REPO

It used to live in the private pipeline, and on 2026-09-13 that cost a run.
The private checkout failed, so every later step was skipped, and the step that
mails the failure was one of them. The run went red and nobody was told, which
is the exact failure this pipeline exists to not have: an alert that shares
fate with the thing it watches is not an alert.

So the mailer is checked out with the workflow itself and depends on nothing
else. Standard library only, on purpose: `pip install` runs after checkout too,
and a step that can be skipped cannot be in the alert path.

Nothing here identifies anything. The subject and body carry labels the run
supplies ("primary", "secondary") and never a host, a path or an address; the
recipient and sender arrive as secrets.

Rules it follows, copied from the pipeline this account already runs:

  * mail on failure, never on success. A job that mails when it worked trains
    you to ignore the mail, and then the one that matters is ignored too.
  * exit non-zero when it mails, so the run goes red as well. Red is the
    signal that survives the mail itself failing.
  * no summary is louder than a bad summary, not quieter. A crash before the
    JSON is written is still an alert.
  * a missing secret is a failure, never a silent no-op.

    python3 scripts/report.py --status "$JOB_STATUS" --json probe.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def summarise(data: dict) -> tuple[list[str], list[str]]:
    """Return (reasons to alert, lines for the body)."""
    reasons: list[str] = []
    lines: list[str] = []

    # URL-map check. It exists because its failure mode is silent: the source
    # changed its url format, every constructed url began returning 404, and
    # three separate wrong explanations were built on that before anyone asked
    # whether the urls were real. A drop here means the crawler is about to
    # work from urls that do not exist.
    # `"checks" not in data` is load-bearing. The site and db suites also
    # write a "total" key, meaning their count of checks, so this branch
    # claimed on 2026-09-27 that the "url map collapsed to 15 entities"
    # on a run where all 15 checks PASSED. A false alarm in the alert
    # path is worse than a missing one: it is the thing that teaches you
    # to stop reading the mail.
    if "checks" not in data and ("ok_rate" in data or "total" in data):
        total = data.get("total")
        if total is not None:
            counts = ", ".join(f"{k} {v}" for k, v in (data.get("counts") or {}).items())
            lines.append(f"url map: {total} entities ({counts})")
        if data.get("error"):
            reasons.append(f"url map could not be built: {data['error']}")
        rate = data.get("ok_rate")
        if rate is not None:
            lines.append(f"sampled {data.get('sampled')}, {data.get('failed')} failed, "
                         f"{rate}% answered 200")
            if rate < 90:
                reasons.append(f"only {rate}% of sampled source urls answered 200; the map "
                               f"is stale or the format changed again")
        if total is not None and total < 50_000:
            reasons.append(f"url map collapsed to {total} entities; it has been ~218,000")

    # Freshness watch. Three things are worth a mail and one is worth a line.
    # A high error rate means the counts cannot be trusted, and the script
    # already exited non-zero for it. Anything that SHRANK needs a human,
    # because it is either the source pulling scenes or our selector breaking,
    # and no code can tell which. A run that checked nothing is a slice that
    # resolved to nobody, which is the map or the counts failing quietly. New
    # scenes are the job working and go in the body, never the subject.
    if data.get("dry_run"):
        # A rehearsal reports coverage and nothing else. Its checked/grew/
        # shrank are all zero by definition, and reading those as a real
        # result would say "checked 0" in a mail about a run that worked.
        lines.append(f"freshness DRY RUN: resolved {data.get('resolved', 0)} of "
                     f"{data.get('resolved', 0) + data.get('unresolved', 0)} "
                     f"sampled, {data.get('catalog_people', 0)} people held. "
                     "Nothing was fetched from the source.")
        if data.get("unresolved"):
            reasons.append(f"{data['unresolved']} held entities are not in the "
                           "source index, so a real run would report them as errors")
    elif "error_rate" in data and "queue" in data:
        s = data.get("summary") or {}
        lines.append(f"freshness: checked {data.get('checked', 0)}, "
                     f"unchanged {s.get('unchanged', 0)}, grew {s.get('grew', 0)} "
                     f"(+{data.get('new_scenes', 0)} scenes), shrank {s.get('shrank', 0)}, "
                     f"errors {s.get('error', 0)} ({round(100 * data['error_rate'])}%)")
        if data.get("checked", 0) == 0:
            reasons.append("freshness watch checked nobody; the slice resolved to no one")
        if data["error_rate"] > 0.10:
            reasons.append(f"freshness watch error rate {round(100 * data['error_rate'])}%; "
                           f"counts are not trustworthy")
        if data.get("shrank"):
            names = ", ".join(r.get("name", "?") for r in data["shrank"][:5])
            reasons.append(f"{len(data['shrank'])} person(s) have fewer scenes at the source "
                           f"than we hold: {names}")
        for r in (data.get("queue") or [])[:10]:
            lines.append(f"  +{r.get('delta')} {r.get('name')} ({r.get('stored')} -> {r.get('live')})")

    # Site checks. Each check is {name, ok, detail}. A failed check is a
    # reason; every check is a body line so a green run still shows what it
    # measured. The check script decides pass/fail against its own ceilings,
    # which are written BELOW the current numbers where a known defect is
    # waiting on a fix, so the run is red until the fix lands, on purpose.
    for c in data.get("checks", []):
        mark = "ok  " if c.get("ok") else "FAIL"
        lines.append(f"  {mark} {c.get('name')}: {c.get('detail', '')}")
        if not c.get("ok"):
            reasons.append(f"{c.get('name')}: {c.get('detail', '')}")
    for r in data.get("results", []):
        name = r.get("source", "?")
        if r.get("aborted"):
            reasons.append(f"{name}: probe aborted ({r['aborted']})")
            lines.append(f"{name}: ABORTED, {r['aborted']}")
            continue
        ok, n = r.get("ok", 0), r.get("attempted", 0)
        rate = f"{ok}/{n}"
        verdict = r.get("verdict", "?")
        lines.append(f"{name}: {rate} answered, verdict {verdict}"
                     + (f", first refusal at request {r['blocked_at']}" if r.get("blocked_at") else ""))
        if r.get("blocked_at"):
            reasons.append(f"{name} refused at request {r['blocked_at']} ({rate} answered)")
        elif n and ok == 0:
            # Every request failed without a refusal being detected. Not a
            # block, but not a working upstream either, and the shape that
            # otherwise reads as "nothing to report".
            reasons.append(f"{name} answered nothing across {n} requests")
    return reasons, lines


def send(key: str, sender: str, to: str, subject: str, body: str) -> None:
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps({"from": sender, "to": [to],
                         "subject": subject, "text": body}).encode(),
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 # Not cosmetic. The API sits behind a CDN that rejects
                 # urllib's default signature with a 403 and a code that says
                 # nothing about mail, so the first run of this mailer failed
                 # for a reason that had nothing to do with the message. Any
                 # non-default agent passes.
                 "User-Agent": "gateway-scheduler/1.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"[report] sent, {r.status}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", required=True, help="the job's own status")
    ap.add_argument("--json", required=True)
    ap.add_argument("--label", default="reachability probe")
    a = ap.parse_args()

    try:
        with open(a.json) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # No summary at all means the job died before writing one, which is
        # itself worth an email: a crash must never be quieter than a refusal.
        reasons, lines = [f"no readable summary was produced ({type(e).__name__})"], []
    else:
        reasons, lines = summarise(data)

    if a.status not in ("success", "Success"):
        reasons.insert(0, f"job status was {a.status}")

    if not reasons:
        print(f"[report] {a.label}: nothing to report")
        for l in lines:
            print(f"  {l}")
        return 0

    subject = f"[gateway] {a.label}: {reasons[0]}"
    body = "\n".join([
        f"A scheduled run needs attention ({a.label}).",
        "",
        "WHY:", *[f"  - {r}" for r in reasons],
        "",
        "RESULT:", *[f"  {l}" for l in (lines or ["(no per-upstream detail)"])],
        "",
        f"Run: {os.environ.get('RUN_URL', '(unknown)')}",
        "Per-request detail is in that run's log.",
    ])

    key = os.environ.get("RESEND_API_KEY")
    to = os.environ.get("ALERT_EMAIL")
    sender = os.environ.get("EMAIL_FROM")
    if not (key and to and sender):
        missing = [n for n, v in (("RESEND_API_KEY", key), ("ALERT_EMAIL", to),
                                  ("EMAIL_FROM", sender)) if not v]
        # Deliberately still a failure. A missing secret silently disabling the
        # alerting is the exact failure this pipeline is built to not have.
        print(f"[report] WOULD send:\n{subject}\n\n{body}")
        print(f"[report] not sent, missing: {', '.join(missing)}")
        return 1

    try:
        send(key, sender, to, subject, body)
    except urllib.error.HTTPError as e:
        # The provider's own words. A mailer that cannot mail and will not say
        # why is the same dead end as no mailer: the first run of this one
        # printed "HTTPError" and nothing else, and the cause took a separate
        # investigation. The body carries no credential, only a complaint about
        # the sender or the payload.
        print(f"[report] NOT SENT, provider returned {e.code}: {e.read(400).decode('utf-8', 'replace')}")
    except (urllib.error.URLError, OSError) as e:
        # Never mask the original problem behind a mail problem.
        print(f"[report] NOT SENT, {type(e).__name__}: {str(e)[:200]}")

    print(f"[report] {subject}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
