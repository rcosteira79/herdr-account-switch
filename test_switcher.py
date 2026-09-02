#!/usr/bin/env python3
"""Checks for the pre-switch liveness check and the badge format.

Run with `python3 test_switcher.py`. Standard library only, same as the plugin.

Nothing here touches a real credential store, a real keychain, or the network:
the state directory is a temporary one, the credential backend is a fake, and
the HTTP layer is replaced. A test that reached any of those could cost a login.
"""
import io
import json
import os
import shutil
import signal
import sys
import tempfile
import time
import urllib.error

STATE = tempfile.mkdtemp(prefix="account-switch-test-")
os.environ["HERDR_PLUGIN_STATE_DIR"] = STATE
os.environ["HERDR_PLUGIN_CONFIG_DIR"] = STATE
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import switcher as S  # noqa: E402

FAILED = []
# Kept so the later checks can put the real ones back after faking them.
REAL_RENEW_PROFILE = S.renew_profile
REAL_FETCH_USAGE = S.fetch_usage
REAL_NOTIFY = S.notify


def check(name, ok, detail=""):
    print(("  ok   " if ok else "  FAIL ") + name
          + (" — " + detail if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def _blocked(*_):
    print("  FAIL the lock deadlocked — a nested _Lock waited on itself")
    os._exit(1)


signal.signal(signal.SIGALRM, _blocked)


class FakeBackend:
    """A credential store in memory. `who` is the account it holds."""

    kind = "claude"
    title = "Fake"

    def __init__(self):
        self.store = None
        self.writes = 0

    def present(self):
        return True

    def read_live(self):
        return self.store

    def write_live(self, payload):
        self.writes += 1
        self.store = payload

    def identity(self, payload):
        return {"account_id": (payload or {}).get("who")}

    def describe(self, identity):
        return str(identity.get("account_id"))


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("https://example.invalid", code, "no", {},
                                  io.BytesIO(body))


def raises(exc):
    def go(*_a, **_k):
        raise exc
    return go


def claude_payload(who, access, refresh, expired=True):
    """A payload shaped like a real claude profile's, for the renewal path."""
    offset = -10 if expired else 9999
    return {"who": who, "credentials": {"claudeAiOauth": {
        "accessToken": access,
        "refreshToken": refresh,
        "expiresAt": int((time.time() + offset) * 1000),
        "scopes": ["user:inference"],
    }}}


def reset(target_payload=None):
    """One live login, one saved profile for it, one saved target profile."""
    for sub in ("profiles", "backups", "rotated"):
        shutil.rmtree(os.path.join(STATE, sub), ignore_errors=True)
    fake = FakeBackend()
    fake.store = claude_payload("LIVE", "live-access", "live-refresh", expired=False)
    S.BACKENDS["claude"] = fake
    S.write_profile(S.make_profile("claude", "Live", dict(fake.store)))
    S.write_profile(S.make_profile(
        "claude", "Target",
        target_payload or claude_payload("TARGET", "old-access", "r1")))
    S.stamp_badges = lambda *a, **k: None
    S.notify = lambda *a, **k: None
    return fake


def switch():
    """Switch to the target profile. Returns (message, refusal)."""
    signal.alarm(5)
    try:
        return S.switch("claude", "target"), None
    except S.SwitchError as exc:
        return None, str(exc)
    finally:
        signal.alarm(0)


# ---- the check itself, with renewal and usage faked ----------------------

print("\na login the provider accepts switches through")
fake = reset()
S.renew_profile = lambda k, p, force=False: p.get("payload")
S.fetch_usage = lambda k, payload: [{"percent": 10}]
msg, err = switch()
check("switch completed", msg is not None, str(err))
check("live store now holds the target", fake.store.get("who") == "TARGET")

print("\na retired login is refused, and nothing is touched")
fake = reset()
asked = []


def refuse_and_count(_kind, _payload):
    asked.append(1)
    raise http_error(401)


S.renew_profile = lambda k, p, force=False: p.get("payload")
S.fetch_usage = refuse_and_count
msg, err = switch()
check("switch refused", msg is None, str(msg))
check("the message names the profile", err and "Target" in err, str(err))
check("the message says nothing changed", err and "nothing changed" in err, str(err))
check("live login untouched", fake.store.get("who") == "LIVE")
check("nothing was written to the store", fake.writes == 0, "writes=%d" % fake.writes)
check("it renewed past the clock and asked again", len(asked) == 2,
      "asked %d times" % len(asked))

print("\nbeing offline never blocks a switch")
fake = reset()
S.renew_profile = lambda k, p, force=False: p.get("payload")
S.fetch_usage = raises(urllib.error.URLError("offline"))
msg, err = switch()
check("switch completed", msg is not None, str(err))
check("live store now holds the target", fake.store.get("who") == "TARGET")

print("\nbeing rate limited never blocks a switch")
fake = reset()
S.renew_profile = lambda k, p, force=False: p.get("payload")
S.fetch_usage = raises(http_error(429))
msg, err = switch()
check("switch completed", msg is not None, str(err))

print("\nthe check can be turned off")
fake = reset()
S.VERIFY_SWITCH = False
S.renew_profile = raises(AssertionError("renewed with the check off"))
S.fetch_usage = raises(AssertionError("asked the provider with the check off"))
msg, err = switch()
check("switch completed without asking the provider", msg is not None, str(err))
S.VERIFY_SWITCH = True

# ---- the same switch, through the real renewal path ----------------------
#
# Only the HTTP layer is replaced here, so renew_profile, fetch_usage and the
# lock nesting inside switch() all run for real.

S.renew_profile = REAL_RENEW_PROFILE
S.fetch_usage = REAL_FETCH_USAGE

print("\nreal renewal path: an expired token is renewed, then installed")
fake = reset()
posted = []
S._post_json = lambda url, body: (posted.append(body), {
    "access_token": "new-access", "refresh_token": "r2", "expires_in": 3600})[1]
S._http_json = lambda url, headers: {"limits": []}
msg, err = switch()
check("switch completed through the nested lock", msg is not None, str(err))
check("it spent the stored refresh token once", len(posted) == 1,
      "posted %d times" % len(posted))
check("it sent the stored refresh token",
      bool(posted) and posted[0].get("refresh_token") == "r1")
installed = fake.store["credentials"]["claudeAiOauth"]
check("installed the renewed access token", installed["accessToken"] == "new-access")
check("installed the rotated refresh token", installed["refreshToken"] == "r2")
parked = S.get_profile("claude", "target")["payload"]["credentials"]["claudeAiOauth"]
check("the profile on disk kept the rotated refresh token",
      parked["refreshToken"] == "r2", json.dumps(parked))
check("no staged payload left behind",
      not os.listdir(os.path.join(STATE, "rotated")))

print("\nreal renewal path: a spent refresh token is refused")
fake = reset()
S._post_json = raises(http_error(
    400, b'{"error":"invalid_grant","error_description":"refresh_token_reused"}'))
S._http_json = lambda url, headers: {"limits": []}
msg, err = switch()
check("switch refused", msg is None, str(msg))
check("the message says nothing changed", err and "nothing changed" in err, str(err))
check("live login untouched", fake.store.get("who") == "LIVE")
check("nothing was written to the store", fake.writes == 0, "writes=%d" % fake.writes)

print("\nthe lock is re-entrant")
with S._Lock():
    signal.alarm(5)
    with S._Lock():
        pass
    signal.alarm(0)
check("a nested lock did not deadlock", True)
check("the lock is released once the outer block ends",
      S._Lock._depth == 0 and S._Lock._file is None)

print("\nthe badge names the agent when it shows more than one account")
S.BACKENDS["claude"] = S.ClaudeBackend()
S.BACKENDS["codex"] = S.CodexBackend()


def badge(fmt, name="work", kind="claude"):
    S.BADGE_FORMAT = fmt
    return S._format_badge(name, kind)


check("the default format is unchanged", badge("{glyph} {name}") == "\N{BUST IN SILHOUETTE} work",
      badge("{glyph} {name}"))
check("{agent} is the agent's short name", badge("{agent} - {name}") == "Claude - work",
      badge("{agent} - {name}"))
check("{title} is its full name", badge("{title}: {name}") == "Claude Code: work",
      badge("{title}: {name}"))
check("{agent} works for codex too",
      badge("{agent} - {name}", "spare", "codex") == "Codex - spare",
      badge("{agent} - {name}", "spare", "codex"))
check("a format spec is honoured", badge("{agent:>7}") == " Claude", repr(badge("{agent:>7}")))
check("an unknown field falls back to the default",
      badge("{nope} {name}") == "\N{BUST IN SILHOUETTE} work", badge("{nope} {name}"))
check("an unbalanced brace falls back to the default",
      badge("{agent {name}") == "\N{BUST IN SILHOUETTE} work", badge("{agent {name}"))
check("a format naming the agent suppresses the prefix",
      S._names_agent("{agent} - {name}") and S._names_agent("{title}: {name}")
      and S._names_agent("{agent:>7}"))
check("a format not naming the agent keeps the prefix",
      not S._names_agent("{glyph} {name}") and not S._names_agent(""))

# ---- what another plugin can read off the profile list -------------------
#
# autocontinue rotates to whichever account frees up soonest, so it has to see
# what each saved account has left. The list used to carry names alone, which
# is why rotation could only take them in the order they were saved.


def list_json(kind="claude"):
    """cmd_list's JSON, captured instead of printed."""
    buffer = io.StringIO()
    real, sys.stdout = sys.stdout, buffer
    try:
        S.cmd_list(["--kind", kind])
    finally:
        sys.stdout = real
    return json.loads(buffer.getvalue())


print("\nthe profile list carries each account's usage")
fake = reset()
S.renew_profile = lambda k, p, force=False: p.get("payload")
asked = []
S.fetch_usage = lambda k, payload: asked.append(1) or []
S._remember_usage("claude:target", [
    {"label": "session", "percent": 100, "resets_at": time.time() + 3600},
    {"label": "weekly", "percent": 40, "resets_at": time.time() + 90000},
])
rows = list_json()
target = next((r for r in rows if r.get("slug") == "target"), None)
check("the parked profile is listed", target is not None, str(rows))
check("its windows come through",
      [w.get("label") for w in (target or {}).get("windows") or []]
      == ["session", "weekly"], str((target or {}).get("windows")))
check("the reading is marked cached, not live",
      (target or {}).get("state") == "cached", str((target or {}).get("state")))
check("its age is there to judge staleness by",
      isinstance((target or {}).get("at"), float), str((target or {}).get("at")))
check("the names it already published are still there",
      (target or {}).get("label") == "Target"
      and (target or {}).get("active") is False, str(target))

print("\nand reads it off the cache, never off the network")
check("a poll-loop caller triggers no fetch", not asked, "%d fetches" % len(asked))

print("\na kind nobody serves still lists nothing")
# usage_rows reads an empty kind list as "every kind", so asking it for the
# profiles of an unknown kind would answer with all of them.
check("an unknown kind returns an empty list", list_json("nosuchkind") == [],
      str(list_json("nosuchkind")))
S.fetch_usage = REAL_FETCH_USAGE
S.renew_profile = REAL_RENEW_PROFILE

# ---- asking the usage report for one kind --------------------------------
#
# Refreshing a reading costs a request per saved account, and a renewal on a
# parked one. A caller that only cares about one harness should not pay for
# the other.


def usage_json(kind=None):
    """cmd_usage's JSON, captured instead of printed."""
    argv = ["--json"] + (["--kind", kind] if kind else [])
    buffer = io.StringIO()
    real, sys.stdout = sys.stdout, buffer
    try:
        S.cmd_usage(argv)
    finally:
        sys.stdout = real
    return json.loads(buffer.getvalue())


print("\nthe usage report can be asked for a single kind")
fake = reset()
S.renew_profile = lambda k, p, force=False: p.get("payload")
S.fetch_usage = lambda k, payload: [{"label": "session", "percent": 5}]
# The second kind is faked as well. An unfiltered report reaches for whatever
# backend is registered, and the real one would read a real credential store.
codex = FakeBackend()
codex.kind = "codex"
S.BACKENDS["codex"] = codex
S.write_profile(S.make_profile("codex", "CodexAcct", {"who": "CODEX"}))
kinds = {r["kind"] for r in usage_json("claude")}
check("only the kind asked for comes back", kinds == {"claude"}, str(kinds))
check("asking for nothing in particular still reports both",
      {r["kind"] for r in usage_json()} == {"claude", "codex"},
      str({r["kind"] for r in usage_json()}))
check("an unknown kind reports nothing", usage_json("nosuchkind") == [],
      str(usage_json("nosuchkind")))
S.fetch_usage = REAL_FETCH_USAGE
S.renew_profile = REAL_RENEW_PROFILE

# ---- which codex windows reach the badge ---------------------------------
#
# Codex reports two: the five-hour one that stops you hour to hour, and the
# weekly one. Only the longest was kept, so a five-hour window sitting at 100%
# was replaced on the badge by a roomy weekly figure — the badge said there was
# capacity on an account that had none.


def codex_row(five_hour, weekly):
    now = time.time()
    return {"kind": "codex", "state": "live", "slug": "acct", "windows": [
        {"label": "5h", "percent": five_hour, "seconds": 18000,
         "resets_at": now + 3600},
        {"label": "weekly", "percent": weekly, "seconds": 604800,
         "resets_at": now + 5 * 86400},
    ]}


print("\nboth codex windows reach the badge")
row = codex_row(100, 60)
labels = [w.get("label") for w in S.shown_windows(row)]
check("neither window is dropped", labels == ["5h", "weekly"], str(labels))
check("the five-hour window is the one shown when it is full",
      (S.summary_window(row) or {}).get("label") == "5h",
      str((S.summary_window(row) or {}).get("label")))
summary = S.usage_summary(row)[0]
check("the badge reports the exhausted window, not the roomy one",
      "100" in summary, summary.strip())
check("both windows are listed in the expanded view",
      len(S.usage_detail(row)) == 2, str(S.usage_detail(row)))

print("\nand it is named the way claude's five-hour window is")
check("5h reads as Session", S.window_name("5h") == "Session", S.window_name("5h"))

print("\nand a fuller weekly window does not displace it")
row = codex_row(10, 90)
check("the short window is still the one shown",
      (S.summary_window(row) or {}).get("label") == "5h",
      str((S.summary_window(row) or {}).get("label")))

print("\nclaude's windows are untouched")
claude = {"kind": "claude", "state": "live", "windows": [
    {"label": "session", "percent": 12}, {"label": "weekly_all", "percent": 57},
    {"label": "weekly Fable", "percent": 21}]}
check("all three still show", len(S.shown_windows(claude)) == 3,
      str(len(S.shown_windows(claude))))

# ---- which window will stop you first ------------------------------------
#
# A percentage means nothing across windows of different lengths: 42% of a week
# is not worse than 10% of five hours. What matters is how long each window has
# before it fills at the rate it is being spent. The badge ranked on the
# percentage, so a five-hour window an hour from stopping you lost to a weekly
# window a day and a half away.


def window(label, percent, seconds, elapsed):
    """A window `elapsed` seconds into its run, at `percent` used."""
    return {"label": label, "percent": percent, "seconds": seconds,
            "resets_at": time.time() + seconds - elapsed}


def codex(session, weekly):
    return {"kind": "codex", "state": "live", "windows": [session, weekly]}


print("\nthe window that fills first is the binding one")
# The numbers measured on a real account: 10% of a five-hour window twelve
# minutes in fills in about an hour and three quarters; 42% of a weekly window
# takes a day and a half.
session = window("5h", 10, 5 * 3600, 12 * 60)
weekly = window("weekly", 42, 168 * 3600, 1601 * 60)
check("the five-hour window fills sooner",
      S.time_to_limit(session) < S.time_to_limit(weekly),
      "%s vs %s" % (S._left(S.time_to_limit(session)),
                    S._left(S.time_to_limit(weekly))))

print("\na window nobody is spending cannot stop anyone")
idle = window("5h", 0, 5 * 3600, 12 * 60)
check("an idle window projects nothing", S.time_to_limit(idle) is None)

print("\na window already spent needs no rate to say so")
spent = window("5h", 100, 5 * 3600, 4 * 3600)
check("it is reached now", S.time_to_limit(spent) == 0)

print("\na window that resets before it fills is not a limit")
slow = window("weekly", 5, 168 * 3600, 100 * 3600)
check("it projects nothing", S.time_to_limit(slow) is None, str(S.time_to_limit(slow)))

print("\none burst at the start of a window is not a rate")
burst = window("5h", 0.5, 5 * 3600, 30)
check("half a percent in thirty seconds projects nothing",
      S.time_to_limit(burst) is None, str(S.time_to_limit(burst)))

print("\nthe figure shown agrees with that window's own line")
row = codex(session, weekly)
shown = S.summary_window(row)
check("the shown window's line names the same limit",
      S.projection(shown).startswith("limit in"), S.projection(shown))

print("\nthe badge counts down to the thing that matters")
# Choosing the right window is half the answer. The badge's own figure was the
# window's reset, so a window two hours from stopping work still read as the
# four and three quarter hours until it refilled.
filling = codex(window("5h", 10, 5 * 3600, 12 * 60),
                window("weekly", 42, 168 * 3600, 1601 * 60))
text = S.usage_summary(filling)[0]
# 90% still to spend at 10% per twelve minutes is an hour and three quarters.
check("a filling window counts down to when it fills", "1h48" in text, text.strip())
check("and not to when it resets", "4h4" not in text, text.strip())

emptied = codex(window("5h", 100, 5 * 3600, 4 * 3600),
                window("weekly", 42, 168 * 3600, 1601 * 60))
text = S.usage_summary(emptied)[0]
check("a spent window counts down to when it lets you back in",
      "1h00" in text or "59m" in text, text.strip())

# ---- a reading that is no longer current ---------------------------------
#
# A cached reading holds its percentage still while the clock keeps running. The
# rate was worked out by dividing that frozen number by the time elapsed until
# *now*, so the longer nobody looked, the calmer a busy account appeared — the
# wrong direction for a figure about what is going to stop you.


def read_ago(seconds, label, percent, window_s, elapsed_at_read):
    """A window read `seconds` ago, `elapsed_at_read` into its own run."""
    read_at = time.time() - seconds
    return {"label": label, "percent": percent, "seconds": window_s,
            "resets_at": read_at + window_s - elapsed_at_read,
            "read_at": read_at}


print("\na rate is measured when the numbers were read")
# 10% twelve minutes into a five-hour window fills in about 1h48 from the read.
# Read an hour ago, that leaves about 48 minutes from now.
stale = read_ago(3600, "5h", 10, 5 * 3600, 12 * 60)
hits = S.time_to_limit(stale)
check("an hour-old reading still projects a limit", hits is not None, str(hits))
check("and counts from now, not from the read",
      hits is not None and 40 * 60 < hits < 56 * 60, S._left(hits))

print("\nthe staleness makes it sooner, never later")
fresh = read_ago(0, "5h", 10, 5 * 3600, 12 * 60)
check("the same numbers read just now are further off",
      hits is not None and S.time_to_limit(fresh) > hits,
      "%s vs %s" % (S._left(S.time_to_limit(fresh)), S._left(hits)))

print("\nand a stale window can outrank a fuller fresh one")
weekly = read_ago(0, "weekly", 42, 168 * 3600, 1601 * 60)
row = {"kind": "codex", "state": "cached", "windows": [stale, weekly]}
check("the five-hour window is still the one shown",
      (S.summary_window(row) or {}).get("label") == "5h",
      str((S.summary_window(row) or {}).get("label")))

print("\na window carrying no read time is read as current")
bare = {"label": "5h", "percent": 10, "seconds": 5 * 3600,
        "resets_at": time.time() + 5 * 3600 - 12 * 60}
check("it behaves exactly as before",
      abs(S.time_to_limit(bare) - S.time_to_limit(fresh)) < 5,
      "%s vs %s" % (S.time_to_limit(bare), S.time_to_limit(fresh)))

print("\nthe cache stamps each window as it stores it")
S._remember_usage("codex:stamped", [{"label": "5h", "percent": 3}])
stored = S._usage_cache()["codex:stamped"]["windows"][0]
check("a stored window carries its read time",
      isinstance(stored.get("read_at"), float), str(stored))

# ---- what the one-line view shows ----------------------------------------
#
# There is room for one figure, and ranking the windows against each other kept
# choosing the wrong one: a window that will not fill projects nothing, so it
# dropped out of the comparison, and a nearly empty weekly window that did
# project beat a session window most of the way through. A real account showed
# 9% with two days left while its session was 58% and six minutes from resetting.
#
# So the short window is the one shown. It is the one met again and again in a
# working day, and a weekly window at 9% is not what stops anybody. A spent
# window is the exception: once one is full the account is blocked, and saying
# so outranks saying how the session is doing.

print("\nthe one-line view shows the short window")
short = window("5h", 37, 5 * 3600, 2 * 3600)
long = window("weekly", 14, 168 * 3600, 20 * 3600)
check("the short window is chosen",
      (S.summary_window(codex(short, long)) or {}).get("label") == "5h",
      str((S.summary_window(codex(short, long)) or {}).get("label")))
check("even when only the long one projects a limit",
      S.time_to_limit(short) is None and S.time_to_limit(long) is not None,
      "short=%s long=%s" % (S.time_to_limit(short), S.time_to_limit(long)))
text = S.usage_summary(codex(short, long))[0]
check("and the figure is the short window's", "37%" in text, text.strip())

print("\nit shows the short window even when nothing is being spent on it")
check("an idle session still shows",
      (S.summary_window(codex(window("5h", 0, 5 * 3600, 60 * 60), long)) or {})
      .get("label") == "5h")

print("\nbut a spent window outranks it, because the account is blocked")
blocked = window("weekly", 100, 168 * 3600, 20 * 3600)
check("the spent window is shown",
      (S.summary_window(codex(window("5h", 49, 5 * 3600, 2 * 3600), blocked)) or {})
      .get("label") == "weekly",
      str((S.summary_window(codex(window("5h", 49, 5 * 3600, 2 * 3600), blocked)) or {})
          .get("label")))

print("\nclaude's three windows pick the session too")
claude = {"kind": "claude", "state": "live", "windows": [
    window("session", 37, 5 * 3600, 2 * 3600),
    window("weekly_all", 14, 168 * 3600, 20 * 3600),
    window("weekly Fable", 0, 168 * 3600, 20 * 3600)]}
check("the session is shown",
      (S.summary_window(claude) or {}).get("label") == "session",
      str((S.summary_window(claude) or {}).get("label")))

# ---- keeping the saved copy of the live account current -------------------
#
# The CLI renews the live tokens continuously, and a fresh `codex login`
# replaces them outright. Only a switch copied that back, so a profile could
# hold a credential the provider had already retired while the live one beside
# it worked — and the next switch would install the dead copy.


def profile_named(label):
    return next(p for p in S.list_profiles("claude") if p["label"] == label)


def age_the_profile(label):
    """Backdate a profile's saved_at, so a fresh write is visible."""
    profile = profile_named(label)
    profile["saved_at"] = 1
    S.write_profile(profile)


print("\nthe live account's saved copy is brought up to date")
fake = reset()
age_the_profile("Live")
fake.store = claude_payload("LIVE", "renewed-access", "renewed-refresh",
                            expired=False)
check("it reports an update", S.sync_live_profile("claude") is True)
saved = profile_named("Live")
check("the saved payload matches the live one", saved["payload"] == fake.store,
      str(saved["payload"].get("credentials", {}).get("claudeAiOauth", {})
          .get("accessToken")))
check("and saved_at moved with it", saved["saved_at"] > 1, str(saved["saved_at"]))

print("\nand left alone when it already matches")
check("nothing is written twice", S.sync_live_profile("claude") is False)

print("\nan account nobody saved is not invented")
fake.store = claude_payload("STRANGER", "a", "b", expired=False)
check("an unsaved live account is skipped",
      S.sync_live_profile("claude") is False)

print("\nnothing logged in is not an error")
fake.store = None
check("it simply reports no update", S.sync_live_profile("claude") is False)

# ---- a renewal is a credential change ------------------------------------
#
# renew_profile replaces a parked profile's tokens, which is exactly the event
# saved_at exists to record. It left the timestamp untouched, so anything
# keyed on saved_at — autocontinue remembers a refused login that way — could
# not tell that the credential had been repaired.

print("\na renewal records that the credential changed")
reset()
age_the_profile("Target")
S._renewed_claude = lambda oauth: dict(oauth, accessToken="brand-new")
S.renew_profile("claude", profile_named("Target"))
saved = profile_named("Target")
token = (saved["payload"]["credentials"]["claudeAiOauth"]["accessToken"])
check("the token was replaced", token == "brand-new", token)
check("and saved_at moved with it", saved["saved_at"] > 1, str(saved["saved_at"]))

# ---- a refused switch has to reach the screen -----------------------------
#
# The picker prints a refusal on its own status line. Every other path — the
# `next` keybinding above all — wrote it to stderr, which herdr drops. The
# failure then read as "the badge did not change", with nothing saying why.
# A polled command must stay silent: `badge` runs once a second from the tab
# bar, and a toast per tick would be worse than the silence it replaces.


def run_main(*argv):
    """main() with these arguments, keeping its stderr out of the output."""
    real_argv, real_err = sys.argv, sys.stderr
    sys.argv = ["switcher.py", *argv]
    sys.stderr = io.StringIO()
    try:
        return S.main()
    finally:
        sys.argv, sys.stderr = real_argv, real_err


def refuse(_argv):
    raise S.SwitchError("codex: Mindera was refused — nothing changed")


print("\na refused switch says so on screen, not only on stderr")
told = []
S.notify = lambda title, body="": told.append((title, body))
S.refresh_stable_link = lambda: None
real_dispatch = dict(S.DISPATCH)
S.DISPATCH["next"] = refuse
S.DISPATCH["badge"] = refuse
code = run_main("next")
check("it still reports failure", code == 1, str(code))
check("it sends one notification", len(told) == 1, str(told))
check("the notification carries the reason",
      bool(told) and "was refused" in told[0][1], str(told))
check("the title says what failed",
      bool(told) and "switch" in told[0][0].lower(), str(told))

print("\nbut a polled command never raises a toast")
told.clear()
code = run_main("badge")
check("nothing was sent", told == [], str(told))
check("it still reports failure", code == 1, str(code))
print("\nand a missing herdr never turns a refusal into a traceback")
told.clear()
S.notify = REAL_NOTIFY
S.herdr = raises(FileNotFoundError(2, "No such file or directory", "herdr"))
code = run_main("next")
check("the refusal is still reported, and nothing is raised", code == 1,
      str(code))
S.DISPATCH.clear()
S.DISPATCH.update(real_dispatch)

shutil.rmtree(STATE, ignore_errors=True)
print("\n%s — %d of the checks failed"
      % ("FAILED" if FAILED else "PASSED", len(FAILED)))
sys.exit(1 if FAILED else 0)
