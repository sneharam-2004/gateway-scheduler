"""watchdog.py - is the schedule itself still alive, and did the checks run?

Two failures this repo cannot see from inside a run, both of which look
exactly like health:

  1. GITHUB DISABLES SCHEDULES ON A PUBLIC REPO after 60 days with no
     repository activity. The workflow stays listed, its last run is green,
     and nothing fires again. A sibling project found every one of its runs
     dead this way and nobody noticed, because nobody looks at a repo that
     has stopped mailing. So this re-enables each workflow on every run: the
     call is idempotent and costs nothing when already enabled.

  2. A RUN THAT DIED BEFORE ITS CHECK STEP still reports a conclusion. If the
     private checkout fails, every later step is skipped, including the one
     that mails, and the run goes red where nobody is watching. Judging a
     workflow by conclusion alone cannot tell "the checks ran and passed"
     from "the checks never ran".

So this judges by STEP, not by badge: the named step must have concluded
success or failure. Skipped, cancelled or null means it never reached the
code, which is reported separately from a check that ran and failed.

JUDGED BY LAST COMPLETED RUN, NEVER BY LAST SUCCESS. A checker that ran and
went red is working; that is its job. Reporting it as "stopped running" buries
the real signal under a duplicate of one you already have.

Standard library only, same as the rest of the alert path, so it cannot be
skipped by a failed pip install.

    python3 scripts/watchdog.py --json watchdog.json
"""
from __future__ import annotations

import argparse
import calendar
import json
import os
import sys
import time
import urllib.error
import urllib.request

REPO = os.environ.get("GITHUB_REPOSITORY", "sneharam-2004/gateway-scheduler")
API = "https://api.github.com"

# The step each workflow must actually reach, and how long it may go quiet.
# A workflow absent from here is still kept enabled but not judged on staleness,
# which is right for the dispatch-only ones.
# The step each workflow must actually reach, and how long it may go quiet.
# A workflow absent from here is still kept enabled but not judged on
# staleness, which is right for the dispatch-only ones.
#
# CEILINGS ARE SET FROM MEASURED DELIVERY, NOT FROM THE CRON. GitHub treats a
# schedule on a public repo as best effort and drops runs under load: measured
# over 30 scheduled site-check runs, a cron asking for every 2 h delivered a
# median gap of 6.2 h, p90 7.4 h and a max of 9.4 h, and 16 of 29 gaps were
# over 6 h. The first ceiling here was 6 h, taken from the cron rather than
# from reality, so it would have cried stale on more than half of a perfectly
# healthy week. 12 h sits above the observed max with headroom and still
# catches a genuinely dead schedule within half a day.
WATCHED = {
    "site-checks.yml": ("Check the live site", 12 * 3600),
    "db-checks.yml": ("Recount the database", 40 * 3600),
}

def gh(path: str, method: str = "GET") -> dict:
    tok = os.environ["GITHUB_TOKEN"]
    req = urllib.request.Request(
        f"{API}{path}",
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "User-Agent": "gateway-watchdog/1.0"},
        method=method)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            # 403 on enable means the token lacks actions:write. That is a
            # failed check, not a quiet skip: without it the keepalive is a
            # no-op and the schedule dies silently in 60 days.
            raise RuntimeError(f"{method} {path} -> {e.code} {e.read()[:120]!r}") from None
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)
    return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", required=True)
    a = ap.parse_args()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {detail}", flush=True)

    try:
        workflows = gh(f"/repos/{REPO}/actions/workflows").get("workflows", [])
    except Exception as e:
        add("watchdog", False, f"could not list workflows: {str(e)[:110]}")
        with open(a.json, "w") as f:
            json.dump({"label": "watchdog", "checks": checks,
                       "failed": 1, "total": 1}, f, indent=1)
        return 1

    for wf in workflows:
        base = wf["path"].rsplit("/", 1)[-1]
        state = wf.get("state")

        # KEEPALIVE. Re-enable unconditionally rather than only when it reads
        # disabled: the state that matters is the one after this call, and one
        # idempotent PUT is cheaper than trusting a field.
        try:
            gh(f"/repos/{REPO}/actions/workflows/{wf['id']}/enable", method="PUT")
            add(f"enabled {base}", True,
                f"was {state}, enable call accepted")
        except Exception as e:
            add(f"enabled {base}", False,
                f"was {state}, enable FAILED so the schedule can still lapse: {str(e)[:80]}")

        if base not in WATCHED:
            continue
        step_name, max_age = WATCHED[base]

        try:
            runs = gh(f"/repos/{REPO}/actions/workflows/{wf['id']}"
                      "/runs?status=completed&per_page=1").get("workflow_runs", [])
        except Exception as e:
            add(f"ran {base}", False, f"could not read runs: {str(e)[:100]}")
            continue
        if not runs:
            add(f"ran {base}", False, "no completed run at all")
            continue

        run = runs[0]
        # calendar.timegm, NOT time.mktime: GitHub stamps UTC and mktime reads
        # its argument as LOCAL time. On this machine that is +5:30, so every
        # age came out 5.5 h too old and a run dispatched minutes earlier
        # reported as 6.1 h stale. That is a daily false alarm, in the one
        # job whose entire purpose is to be believed.
        age = time.time() - calendar.timegm(
            time.strptime(run["updated_at"], "%Y-%m-%dT%H:%M:%SZ"))
        jobs = gh(f"/repos/{REPO}/actions/runs/{run['id']}/jobs").get("jobs", [])
        steps = [s for j in jobs for s in j.get("steps", [])]
        named = [s for s in steps if s.get("name") == step_name]
        reached = any(s.get("conclusion") in ("success", "failure") for s in named)

        if not reached:
            got = ", ".join(str(s.get("conclusion")) for s in named) or "step absent"
            add(f"ran {base}", False,
                f"last run {run['id']} concluded {run['conclusion']} but "
                f"'{step_name}' never ran ({got}), so the checks did not execute")
        elif age > max_age:
            add(f"ran {base}", False,
                f"last completed run was {age / 3600:.1f} h ago, over the "
                f"{max_age / 3600:.0f} h ceiling; the schedule may have lapsed")
        else:
            add(f"ran {base}", True,
                f"'{step_name}' concluded, {age / 3600:.1f} h ago, "
                f"run was {run['conclusion']}")

    failed = [c for c in checks if not c["ok"]]
    with open(a.json, "w") as f:
        json.dump({"label": "watchdog", "checks": checks,
                   "failed": len(failed), "total": len(checks)}, f, indent=1)
    print(f"{len(checks) - len(failed)}/{len(checks)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
