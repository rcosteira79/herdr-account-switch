#!/usr/bin/env python3
"""Checks for the pre-switch liveness check.

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

shutil.rmtree(STATE, ignore_errors=True)
print("\n%s — %d of the checks failed"
      % ("FAILED" if FAILED else "PASSED", len(FAILED)))
sys.exit(1 if FAILED else 0)
