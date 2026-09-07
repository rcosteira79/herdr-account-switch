---
date: 2026-09-07
problem_type: runtime_error
component: usage
tags: [claude, usage, rate-limit, cache]
---

# Evidence

Mindera's Claude session hit its usage limit while the picker showed 0%.
Its cached session reading was about two hours old and a `retry_after` entry
showed that usage reads had been throttled. Personal had a recent reading.

A direct, read-only comparison using the saved, unexpired tokens then returned:

- Personal: HTTP 429, `Retry-After: 201`, `rate_limit_error` with the message
  `Rate limited. Please try again later.`
- Mindera: HTTP 200, session 100%, weekly 65%, model weekly 52%.

No token refresh or login change was required. This establishes that Mindera's
token and the parser can retrieve its real usage, and that Personal can also
be throttled. It does not establish the provider's rate-limit scope or why
Mindera's reporting requests were throttled for longer.

The installed ccstatusline can query the same endpoint, with its own 180-second
cache, when Claude's status-line input lacks required usage fields. This is
another possible source of reporting requests, not proof that it caused this
incident. Its cache is token-keyed; never attribute its values to an account
merely because that account is currently selected.

# Local defects and changes

- The switch verifier fetched usage and discarded it. Successful verification
  now saves the reading, and a 429 records its cooldown for usage readers.
- An open picker never fetched again unless the user pressed `u`. Both usage
  views now refresh at the cache interval, respecting each account's backoff.
- Every 429 previously imposed a fixed five-minute wait. The switcher now
  honors numeric and HTTP-date `Retry-After` values, with five minutes as the
  fallback for missing or unusable values.
- Old percentages were displayed without age or error in the picker. The
  collapsed view keeps the bar and countdown with a `~` before the last known
  percentage; expanded usage shows the age and refresh error. An earlier fix
  replaced the percentage with the error, which hid useful capacity information.

Regression checks in `test_switcher.py` use fake HTTP and credential stores.
They cover saving and reusing verification reads, sharing verification backoff,
retry delays, stale-zero presentation, and aging a reading while the picker
remains open. These changes improve recovery and reduce duplicate reads; they
cannot guarantee that Anthropic's reporting endpoint will accept a request.

# Independent collection and concurrent readers

A later attempt imported ccstatusline's token-matched local cache. That was
removed: this plugin must collect usage independently of status-line plugins.
The switcher uses the authenticated provider endpoint and its own cache only.

Code inspection found that concurrent picker/usage processes could both read
an expired cache before either recorded its HTTP result, causing duplicate
requests. Usage collection now takes the existing re-entrant process lock
before reading the cache, fetching, and recording a result or cooldown. A
waiting reader therefore sees the preceding reader's update. Both panes also
reload the plugin's own cache on UI ticks to observe reads from other actions.

Regression tests start two processes simultaneously with fake HTTP and verify
exactly one request per account, both for successful reads and HTTP 429s.
This proves request deduplication within the switcher, not why Anthropic
throttled an individual account. Other clients can still query that endpoint,
and provider throttling remains possible.
