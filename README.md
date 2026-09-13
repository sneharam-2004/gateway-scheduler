# gateway-scheduler

Scheduled workflows. No application code, no data, and nothing that identifies
what they talk to.

## Why this is a separate repo

Actions minutes bill to the account that **owns the repository**, and standard
runners are free and unmetered on **public** repositories. The pipeline itself
has to stay private, because it carries upstream targets, selectors and
credentials. One repo cannot be both, so there are two:

| repo | visibility | holds |
|---|---|---|
| this one | public | workflows, and the mailer they alert with |
| the pipeline | private | code, targets, selectors, data |

Each workflow checks this repo out at the root, mounts the private repo at
`pipeline/`, and calls into it. That is all these files do.

## Nothing here is identifying

Not a hostname, a path, a selector, a project name or an email address, and not
in a comment either. Everything specific arrives at run time:

| arrives as | what |
|---|---|
| `vars.PRIVATE_REPO` | the repo to check out |
| `secrets.PIPELINE_DEPLOY_KEY` | reads it |
| `secrets.ALERT_EMAIL`, `secrets.EMAIL_FROM` | who is told when something breaks |
| the private repo | every upstream host and selector |

`scripts/report.py` is the one piece of code here, and it is the exception that
proves the rule: it formats and sends the failure mail using only labels the run
hands it. No artifacts are uploaded, because an artifact on a public repository
is world-downloadable and the run data records the URL of every request.

The repo name follows the same rule as the machine names in this fleet: it
describes nothing. A change that puts a target, a brand or an address in here
defeats the whole split, so treat it the way you would treat committing a key.

## Setup

Variable:

| name | value |
|---|---|
| `PRIVATE_REPO` | `owner/name` of the private pipeline |

Secrets:

| name | what |
|---|---|
| `PIPELINE_DEPLOY_KEY` | read-only deploy key for the private repo, private half |
| `RESEND_API_KEY` | same provider the sibling pipeline already uses |
| `ALERT_EMAIL` | where failures go |
| `EMAIL_FROM` | verified Resend sender |

A deploy key rather than a PAT, chosen after a PAT cost two days. A
fine-grained PAT's access is the product of two independent account-level
settings, neither readable nor writable from the API, and a wrong one fails as
a bare `404` that looks identical to a missing repo. A deploy key is scoped to
one repository, is read-only, lives in that repository's own settings, and is
revocable with `gh repo deploy-key delete` without affecting anything else.

Mail is an explicit step in each workflow rather than a notification setting,
because GitHub's own failure notices go to the repo owner's address, which is
not an address anyone reads.

## Rules these workflows follow

**Mail on failure, never on success.** A job that mails when it worked trains
you to ignore the mail, and then the one that matters is ignored too.

**Red as well as mail.** Alert steps exit non-zero so the run goes red. Red is
the signal that survives the mail itself failing.

**A watchdog that cannot share fate with what it watches.** A runner that dies
takes its own failure-mail step down with it. That has happened on the sibling
pipeline and nobody was told, so a separate job on a separate schedule asks the
only question that survives it: when did this workflow last *succeed*?

The first run here failed the same way in miniature. The mailer lived in the
private repo, the private checkout failed, and every later step was skipped
including the one that mails. Red run, empty inbox. Hence the rule that decides
where code goes in these workflows: **an alert step may only use what is present
before the first step that can fail.** Not the private checkout, not
`setup-python`, not `pip install`. Standard library, checked out with the
workflow itself.

**Silence is not success.** "0 new, 0 failed" is what a healthy day and a
completely broken crawler both look like. Attempts, answers and refusals are
counted separately, and an unexplained run of zeroes alerts.

## Probe before schedule

`probe.yml` is dispatch-only and answers whether a hosted runner can reach the
upstreams at all. Measured elsewhere on 2026-09-13: one answers 6 of 10
requests at 10-second intervals from a datacenter address and resets the rest,
the other answers everything, and a residential address is refused outright. A
hosted runner is a different address again, so it gets measured rather than
assumed. Nothing goes on a cron against the throttled upstream until it reports.
