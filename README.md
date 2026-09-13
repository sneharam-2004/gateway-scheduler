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
| this one | public | workflows only, so the minutes are free |
| the pipeline | private | code, targets, selectors, data |

Each workflow checks the private repo out at run time and calls into it. That
is all these files do.

## Nothing here is identifying

Not a hostname, a path, a selector, a project name or an email address, and not
in a comment either. Everything specific arrives at run time:

| arrives as | what |
|---|---|
| `vars.PRIVATE_REPO` | the repo to check out |
| `secrets.CI_CHECKOUT_PAT` | reads it |
| `secrets.ALERT_EMAIL`, `secrets.EMAIL_FROM` | who is told when something breaks |
| the private repo | every upstream host and selector |

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
| `CI_CHECKOUT_PAT` | fine-grained PAT, `contents: read` on the private repo only |
| `RESEND_API_KEY` | same provider the sibling pipeline already uses |
| `ALERT_EMAIL` | where failures go |
| `EMAIL_FROM` | verified Resend sender |

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
