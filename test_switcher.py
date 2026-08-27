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
check("the five-hour window is the binding one when it is full",
      (S.binding_window(row) or {}).get("label") == "5h",
      str((S.binding_window(row) or {}).get("label")))
summary = S.usage_summary(row)[0]
check("the badge reports the exhausted window, not the roomy one",
      "100" in summary, summary.strip())
check("both windows are listed in the expanded view",
      len(S.usage_detail(row)) == 2, str(S.usage_detail(row)))

print("\nand it is named the way claude's five-hour window is")
check("5h reads as Session", S.window_name("5h") == "Session", S.window_name("5h"))

print("\nthe weekly window still wins when it is the fuller one")
row = codex_row(10, 90)
check("the weekly window binds", (S.binding_window(row) or {}).get("label") == "weekly",
      str((S.binding_window(row) or {}).get("label")))

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
check("and it is the one the badge reports",
      (S.binding_window(codex(session, weekly)) or {}).get("label") == "5h",
      str((S.binding_window(codex(session, weekly)) or {}).get("label")))

print("\na window nobody is spending cannot stop anyone")
idle = window("5h", 0, 5 * 3600, 12 * 60)
check("an idle window projects nothing", S.time_to_limit(idle) is None)
check("so the weekly window binds instead",
      (S.binding_window(codex(idle, weekly)) or {}).get("label") == "weekly",
      str((S.binding_window(codex(idle, weekly)) or {}).get("label")))

print("\na window already spent stops you now")
spent = window("5h", 100, 5 * 3600, 4 * 3600)
check("it needs no rate to say so", S.time_to_limit(spent) == 0)
check("and it outranks anything still filling",
      (S.binding_window(codex(spent, weekly)) or {}).get("label") == "5h",
      str((S.binding_window(codex(spent, weekly)) or {}).get("label")))

print("\na window that resets before it fills is not a limit")
slow = window("weekly", 5, 168 * 3600, 100 * 3600)
check("it projects nothing", S.time_to_limit(slow) is None, str(S.time_to_limit(slow)))

print("\none burst at the start of a window is not a rate")
burst = window("5h", 0.5, 5 * 3600, 30)
check("half a percent in thirty seconds projects nothing",
      S.time_to_limit(burst) is None, str(S.time_to_limit(burst)))

print("\nthe words and the ranking cannot disagree")
row = codex(session, weekly)
binding = S.binding_window(row)
check("the binding window is the one whose line names a limit",
      S.projection(binding).startswith("limit in"), S.projection(binding))

shutil.rmtree(STATE, ignore_errors=True)
print("\n%s — %d of the checks failed"
      % ("FAILED" if FAILED else "PASSED", len(FAILED)))
sys.exit(1 if FAILED else 0)
