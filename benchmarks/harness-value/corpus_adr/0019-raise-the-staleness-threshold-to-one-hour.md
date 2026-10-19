# 0019. Raise the staleness threshold to one hour

## Status

Accepted. Supersedes 0007.

## Date

2025-07-23

## Context

0007 marks every unit stale when the repository's last fetch (the mtime of
`FETCH_HEAD`, per 0006) is more than 15 minutes old. 0007 said explicitly that
15 minutes was a guess made before we had usage data and that it should be
revisited once we did. We now have it.

Over two weeks, four regular operators ran a build of landfirst that logged, per
repository, the time of each fetch and the time the panel was open. Findings:

- The **median interval between fetches was 38 minutes**. Operators fetch when
  they start a session, after an agent reports finishing, and when something
  looks wrong; they do not fetch on a schedule.
- With a 15-minute threshold, units were marked stale for about 70% of the time
  the panel was open. In interviews, every operator said they had stopped
  looking at the stale marker within the first few days, which is the warning
  fatigue 0007 anticipated.
- The cases where the marker mattered (a branch pushed from another machine
  reading as never pushed) involved views of the remote that were hours old,
  typically from the previous day. None of the reported false alarms involved a
  fetch between 15 and 60 minutes old.

A marker that is on most of the time is ignored, including on the occasions it
is right. The threshold should sit above the normal fetch rhythm so that the
marker means "this is unusually old", not "you have not fetched in the last few
minutes".

Options considered:

- **30 minutes.** Still below the measured median of 38 minutes, so the marker
  would still be on for a large share of the time. Rejected.
- **1 hour.** Above the median and comfortably above most intervals in the
  logs, while still well short of the "previous day" views that caused the real
  false alarms.
- **4 hours.** Would clear the marker almost all the time, but a view from the
  start of a morning session would read as fresh at lunchtime, which is
  exactly when stale verdicts cause problems. Rejected.
- **Fetch automatically in the background** so staleness never arises. Rejected
  again for the reasons in 0006: network, credential prompts, and changing the
  observed state mid-build.

## Decision

The staleness threshold is **one hour** (`time.Hour`). A unit is stale when the
last fetch is more than an hour old or the repository has never fetched. All
other aspects of 0007 (applied uniformly to every unit, zero time treated as
stale without a special case) are unchanged.

The threshold remains a constant rather than a flag. We now have one data point
about operator behaviour, not enough to know which other values people would
need.

## Consequences

- In the logged sessions, the marker would have been on for about 15% of panel
  time instead of 70%, concentrated in the first minutes of a session before the
  first fetch, which is when it is most useful.
- A branch pushed from another machine less than an hour ago can still read as
  never pushed without a stale marker. We accept that; the marker was being
  ignored in that window anyway.
- Fixtures in tests are still never fetched, so they are still always stale.
- If the threshold needs to differ per repository, that is a new decision
  (configuration), not an edit to this one.
