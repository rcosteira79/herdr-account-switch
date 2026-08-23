#!/usr/bin/env python3
"""Hot-swap Claude Code / Codex accounts from inside herdr.

Both CLIs re-read their credential store on every request, so swapping the
stored login switches which account your *already running* agents bill to —
no logout, no browser round-trip, no restarting panes. This plugin keeps named
snapshots of those credential stores and swaps them atomically.

What a profile holds (per agent kind):
  claude  the OAuth credential blob (macOS Keychain item "Claude Code-credentials",
          or ~/.claude/.credentials.json elsewhere) plus the `oauthAccount`
          identity block out of ~/.claude.json
  codex   ~/.codex/auth.json

Profiles live in HERDR_PLUGIN_STATE_DIR/profiles/<kind>/<slug>.json, 0600 under
a 0700 directory. They are password-equivalent secrets: same value as the files
they came from, in the same place on the same machine, and never leave it.

Subcommands:
  ui       the overlay picker (plugin pane entrypoint)
  open     open that pane (action)
  next     switch to the next profile of the focused agent's kind (action)
  save     snapshot the current login into a new profile (action)
  status   print what is logged in and what is saved (action)
  stamp    refresh the $acct pane badges (startup hook)
  switch   switch <kind> <label>  — the scriptable form, for keybindings
"""
import base64
import fcntl
import getpass
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
import time

PLUGIN_ID = os.environ.get("HERDR_PLUGIN_ID", "rcosteira.account-switch")
HERDR = os.environ.get("HERDR_BIN_PATH", "herdr")
# The fallback has to match the directory herdr itself uses, because the badge
# command runs from `tab_bar_right`, which passes no plugin state dir.
STATE_DIR = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
    os.path.join("~/.local/state/herdr/plugins", PLUGIN_ID)
)
PROFILES_DIR = os.path.join(STATE_DIR, "profiles")
BACKUP_DIR = os.path.join(STATE_DIR, "backups")
LOCK = os.path.join(STATE_DIR, "switch.lock")
# An installed plugin's directory carries a content hash, so it moves on every
# update and anything naming that path breaks silently. This symlink lives in
# the state directory, which is keyed by plugin id and therefore stable, and is
# repointed at each startup. Config can name it and survive updates.
# realpath, not abspath: this script is *meant* to be run through the symlink
# below, and abspath would then report the symlink's own directory — so the
# refresh would point the link at itself and nothing could be read through it.
ROOT = os.path.dirname(os.path.realpath(__file__))
STABLE_LINK = os.path.join(STATE_DIR, "current")

TOKEN = "acct"
UNSAVED_MARK = "*"  # live login that no profile has a copy of
KEEP_BACKUPS = 10
IS_MAC = platform.system() == "Darwin"


CONFIG_DIR = os.environ.get("HERDR_PLUGIN_CONFIG_DIR") or STATE_DIR
PREFIX = "ACCOUNT_SWITCH_"


def _load_settings():
    """Settings from the plugin's own config dir.

    Actions inherit the herdr server's environment, so an environment variable
    can only be set by restarting herdr with it exported. A file in the config
    dir is the one place a person can actually change these.
    """
    for name in ("config.toml", "config.json"):
        path = os.path.join(CONFIG_DIR, name)
        if not os.path.exists(path):
            continue
        try:
            if name.endswith(".toml"):
                import tomllib
                with open(path, "rb") as handle:
                    return tomllib.load(handle)
            with open(path, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            continue  # a broken config must not stop the picker
    return {}


SETTINGS = _load_settings()


def _setting(name):
    """The environment first, then the config file, then nothing.

    The config key is the variable without its prefix, lowercased:
    ACCOUNT_SWITCH_USAGE_RENEW is `usage_renew`.
    """
    if name in os.environ:
        return os.environ[name]
    key = name[len(PREFIX):].lower() if name.startswith(PREFIX) else name.lower()
    return SETTINGS.get(key)


def _flag(name, default=True):
    value = _setting(name)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _num_env(name, default):
    try:
        return float(_setting(name))
    except (TypeError, ValueError):
        return default


NOTIFY = _flag("ACCOUNT_SWITCH_NOTIFY")
BADGE = _flag("ACCOUNT_SWITCH_BADGE")
# Badge a kind with no saved profile too.
BADGE_ALWAYS = _flag("ACCOUNT_SWITCH_BADGE_ALWAYS", default=False)
GLYPH = _setting("ACCOUNT_SWITCH_GLYPH") or "\N{BUST IN SILHOUETTE}"  # 👤
# Both fields are optional: "{glyph}" is a badge with no text, "{name}" is text
# with no glyph.
BADGE_FORMAT = _setting("ACCOUNT_SWITCH_BADGE_FORMAT") or "{glyph} {name}"
SEPARATOR = _setting("ACCOUNT_SWITCH_SEPARATOR") or " · "


class SwitchError(Exception):
    """A switch that could not be completed (live store left untouched)."""


# ---- herdr plumbing -------------------------------------------------------

def herdr(*args):
    """Run the herdr CLI; return CompletedProcess (never raises on non-zero)."""
    return subprocess.run(
        [HERDR, *args], capture_output=True, text=True, check=False
    )


def live_agents():
    """pane_id -> agent info, or None when the herdr server is unreachable
    (distinct from an empty session)."""
    res = herdr("agent", "list")
    if res.returncode != 0:
        return None
    try:
        agents = json.loads(res.stdout)["result"]["agents"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
    return {a["pane_id"]: a for a in agents if a.get("pane_id")}


def notify(title, body=""):
    if not NOTIFY:
        return
    args = ["notification", "show", title, "--sound", "none"]
    if body:
        args += ["--body", body]
    herdr(*args)


class _Lock:
    """Serialise credential swaps: two overlapping writes to the same store
    would race the CLIs' own token refresh.

    Re-entrant, because the nesting is real: switch() holds this lock while
    proven_payload renews, and renew_profile takes it again. flock keys on the
    open file rather than on the process, so a second open() inside the first
    would queue behind a lock this very process already holds, and wait for
    ever.
    """

    _depth = 0
    _file = None

    def __enter__(self):
        if _Lock._depth == 0:
            _secure_dir(STATE_DIR)
            _Lock._file = open(LOCK, "w")
            fcntl.flock(_Lock._file, fcntl.LOCK_EX)
        _Lock._depth += 1
        return self

    def __exit__(self, *exc):
        _Lock._depth -= 1
        if _Lock._depth == 0 and _Lock._file is not None:
            fcntl.flock(_Lock._file, fcntl.LOCK_UN)
            _Lock._file.close()
            _Lock._file = None


# ---- small file helpers ---------------------------------------------------

def _secure_dir(path):
    """Create one of *our* state directories, owner-only. Never used on
    directories we don't own (~/.claude, $HOME) — their modes aren't ours."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_json_secret(path, data, mode=0o600):
    """Atomic, mode-preserving write of a credential file."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _slug(text):
    s = re.sub(r"[^a-z0-9._-]+", "-", (text or "").lower()).strip("-.")
    return s or "profile"


def _fingerprint(identity):
    """Stable id for "which account is this". Built from the account uuid /
    email only — never from tokens, which rotate on every refresh."""
    key = identity.get("account_id") or identity.get("email")
    if not key:
        return None
    return hashlib.sha256(str(key).encode()).hexdigest()[:16]


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}


# ---- credential backends --------------------------------------------------

class Backend:
    kind = ""
    title = ""

    def present(self):
        """True when this agent looks configured on this machine."""
        raise NotImplementedError

    def read_live(self):
        """Current credential payload, or None when nothing is logged in."""
        raise NotImplementedError

    def write_live(self, payload):
        """Install a payload as the live login. Raises SwitchError on failure."""
        raise NotImplementedError

    def identity(self, payload):
        """Human/stable identity fields for a payload."""
        raise NotImplementedError

    def describe(self, identity):
        bits = [identity.get("email") or identity.get("account_id") or "unknown"]
        if identity.get("plan"):
            bits.append(identity["plan"])
        if identity.get("org"):
            bits.append(identity["org"])
        return " · ".join(str(b) for b in bits if b)


class ClaudeBackend(Backend):
    kind = "claude"
    title = "Claude Code"
    KEYCHAIN_SERVICE = "Claude Code-credentials"

    def __init__(self):
        cfg = os.environ.get("CLAUDE_CONFIG_DIR")
        if cfg:
            self.home = os.path.expanduser(cfg)
            self.settings = os.path.join(self.home, ".claude.json")
        else:
            self.home = os.path.expanduser("~/.claude")
            self.settings = os.path.expanduser("~/.claude.json")
        self.creds_file = os.path.join(self.home, ".credentials.json")

    def present(self):
        return os.path.isdir(self.home) or os.path.exists(self.settings)

    # -- keychain (macOS) --
    def _keychain_read(self):
        for args in (
            ["-a", getpass.getuser(), "-w"],
            ["-w"],  # some installs store it under a different account name
        ):
            r = subprocess.run(
                ["security", "find-generic-password", "-s", self.KEYCHAIN_SERVICE, *args],
                capture_output=True, text=True, check=False,
            )
            if r.returncode == 0 and r.stdout.strip():
                try:
                    return json.loads(r.stdout.strip())
                except json.JSONDecodeError:
                    return None
        return None

    def _keychain_write(self, data):
        blob = json.dumps(data, separators=(",", ":"))
        base = [
            "security", "add-generic-password", "-U",
            "-s", self.KEYCHAIN_SERVICE, "-a", getpass.getuser(),
        ]
        # Prefer feeding the secret on stdin so it never lands in `ps` output;
        # fall back to the argv form if this `security` build won't take it.
        attempts = [
            (base + ["-w"], blob + "\n"),
            (base + ["-w", blob], None),
        ]
        for argv, stdin in attempts:
            subprocess.run(
                argv, input=stdin, capture_output=True, text=True, check=False
            )
            if self._keychain_read() == data:
                return True
        return False

    # -- payload --
    def _read_creds(self):
        """(payload, store) for whichever store actually holds the login."""
        if IS_MAC:
            kc = self._keychain_read()
            if kc is not None:
                return kc, "keychain"
        f = _read_json(self.creds_file)
        if f is not None:
            return f, "file"
        return None, None

    def _oauth_account(self):
        settings = _read_json(self.settings) or {}
        acct = settings.get("oauthAccount")
        return acct if isinstance(acct, dict) else None

    def read_live(self):
        creds, store = self._read_creds()
        if creds is None:
            return None
        return {
            "store": store,
            "credentials": creds,
            "oauth_account": self._oauth_account(),
        }

    def write_live(self, payload):
        creds = payload.get("credentials")
        if not creds:
            raise SwitchError("profile holds no claude credentials")

        # Write to whatever store is live now; if nothing is logged in, fall
        # back to the store the profile was captured from.
        _, live_store = self._read_creds()
        store = live_store or payload.get("store") or ("keychain" if IS_MAC else "file")

        if store == "keychain":
            if not self._keychain_write(creds):
                raise SwitchError(
                    "could not write the Keychain item 'Claude Code-credentials' "
                    "(unlock the login keychain and retry)"
                )
        else:
            _write_json_secret(self.creds_file, creds)

        # Identity block: patch only `oauthAccount`, everything else in
        # ~/.claude.json (projects, history, onboarding) is not ours to touch.
        acct = payload.get("oauth_account")
        if acct:
            settings = _read_json(self.settings)
            if settings is None:
                settings = {}
            settings["oauthAccount"] = acct
            mode = 0o600
            try:
                mode = os.stat(self.settings).st_mode & 0o777
            except OSError:
                pass
            _write_json_secret(self.settings, settings, mode=mode)

    def identity(self, payload):
        acct = payload.get("oauth_account") or {}
        oauth = (payload.get("credentials") or {}).get("claudeAiOauth") or {}
        return {
            "email": acct.get("emailAddress"),
            "account_id": acct.get("accountUuid"),
            "org": acct.get("organizationName"),
            "plan": oauth.get("subscriptionType"),
        }


class CodexBackend(Backend):
    kind = "codex"
    title = "Codex"

    def __init__(self):
        self.home = os.path.expanduser(os.environ.get("CODEX_HOME") or "~/.codex")
        self.auth = os.path.join(self.home, "auth.json")

    def present(self):
        return os.path.isdir(self.home)

    def read_live(self):
        data = _read_json(self.auth)
        return {"auth": data} if data is not None else None

    def write_live(self, payload):
        auth = payload.get("auth")
        if not auth:
            raise SwitchError("profile holds no codex credentials")
        _write_json_secret(self.auth, auth)

    def identity(self, payload):
        auth = payload.get("auth") or {}
        tokens = auth.get("tokens") or {}
        claims = _jwt_claims(tokens.get("id_token", "")) if tokens else {}
        openai = claims.get("https://api.openai.com/auth") or {}
        account_id = (
            tokens.get("account_id")
            or openai.get("chatgpt_account_id")
            or (("apikey:" + hashlib.sha256(
                auth["OPENAI_API_KEY"].encode()).hexdigest()[:12])
                if auth.get("OPENAI_API_KEY") else None)
        )
        return {
            "email": claims.get("email"),
            "account_id": account_id,
            "org": None,
            "plan": openai.get("chatgpt_plan_type") or auth.get("auth_mode"),
        }


BACKENDS = {b.kind: b for b in (ClaudeBackend(), CodexBackend())}
KIND_ORDER = ["claude", "codex"]


def kind_of_agent(info):
    """Map a herdr agent label ("Claude Code", "codex", ...) to a backend."""
    label = " ".join(
        str(info.get(k) or "") for k in ("agent", "name", "display_agent")
    ).lower()
    for kind in KIND_ORDER:
        if kind in label:
            return kind
    return None


# ---- profile store --------------------------------------------------------

def _kind_dir(kind):
    return os.path.join(PROFILES_DIR, kind)


def list_profiles(kind):
    """Saved profiles for a kind, oldest-saved first (that's the cycle order)."""
    out = []
    try:
        names = sorted(os.listdir(_kind_dir(kind)))
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        data = _read_json(os.path.join(_kind_dir(kind), name))
        if isinstance(data, dict) and data.get("payload"):
            data.setdefault("slug", name[:-5])
            data.setdefault("label", data["slug"])
            out.append(data)
    out.sort(key=lambda p: (p.get("saved_at") or 0, p["slug"]))
    return out


def get_profile(kind, key):
    """Look a profile up by slug or (case-insensitive) label."""
    for p in list_profiles(kind):
        if key in (p["slug"], p["label"]) or key.lower() == p["label"].lower():
            return p
    return None


def write_profile(profile):
    _secure_dir(PROFILES_DIR)
    _secure_dir(_kind_dir(profile["kind"]))
    path = os.path.join(_kind_dir(profile["kind"]), profile["slug"] + ".json")
    _write_json_secret(path, profile)
    return path


def delete_profile(kind, slug):
    try:
        os.remove(os.path.join(_kind_dir(kind), slug + ".json"))
        return True
    except OSError:
        return False


def _unique_slug(kind, label):
    base = _slug(label)
    slug, n = base, 2
    existing = {p["slug"] for p in list_profiles(kind)}
    while slug in existing:
        slug = f"{base}-{n}"
        n += 1
    return slug


def make_profile(kind, label, payload):
    backend = BACKENDS[kind]
    identity = backend.identity(payload)
    return {
        "kind": kind,
        "slug": _unique_slug(kind, label),
        "label": label,
        "identity": identity,
        "fingerprint": _fingerprint(identity),
        "saved_at": int(time.time()),
        "last_used": None,
        "payload": payload,
    }


def default_label(kind, identity):
    email = identity.get("email") or ""
    if "@" in email:
        return email.split("@", 1)[0]
    if identity.get("account_id"):
        return str(identity["account_id"])[:8]
    return f"{kind}-{int(time.time())}"


def active_profile(kind, live=None):
    """The saved profile matching the live login, or None."""
    backend = BACKENDS[kind]
    if live is None:
        live = backend.read_live()
    if live is None:
        return None
    fp = _fingerprint(backend.identity(live))
    if not fp:
        return None
    for p in list_profiles(kind):
        if p.get("fingerprint") == fp:
            return p
    return None


def _backup(kind, payload):
    _secure_dir(BACKUP_DIR)
    path = os.path.join(BACKUP_DIR, f"{kind}-{int(time.time())}.json")
    _write_json_secret(path, payload)
    kept = sorted(
        f for f in os.listdir(BACKUP_DIR) if f.startswith(kind + "-")
    )
    for stale in kept[:-KEEP_BACKUPS]:
        try:
            os.remove(os.path.join(BACKUP_DIR, stale))
        except OSError:
            pass
    return path


# ---- the switch itself ----------------------------------------------------

def switch(kind, key):
    """Install a saved profile as the live login. Returns a status string."""
    backend = BACKENDS[kind]
    with _Lock():
        target = get_profile(kind, key)
        if target is None:
            raise SwitchError(f"no {kind} profile named {key!r}")

        live = backend.read_live()
        live_fp = None
        if live is not None:
            live_fp = _fingerprint(backend.identity(live))
            if live_fp and live_fp == target.get("fingerprint"):
                return f"{kind}: already on {target['label']}"

        # Prove the saved login still works before overwriting a working one.
        # This can rotate the target's tokens, so keep what it hands back: the
        # write below installs it, and the profile is rewritten from it at the
        # end of the switch.
        target["payload"] = proven_payload(kind, target)

        if live is not None:
            # Never overwrite a login we don't have a copy of.
            outgoing = next(
                (p for p in list_profiles(kind) if p.get("fingerprint") == live_fp),
                None,
            )
            if outgoing is None:
                ident = backend.identity(live)
                rescued = make_profile(kind, "autosaved-" + default_label(kind, ident), live)
                write_profile(rescued)
            else:
                # Refresh the snapshot before parking it. The CLI keeps renewing
                # the live tokens, so a profile saved hours ago holds older ones.
                # Restoring a stale copy can leave that account unable to renew,
                # which costs a browser login — and rotating makes that routine.
                outgoing["payload"] = live
                outgoing["identity"] = backend.identity(live)
                outgoing["saved_at"] = int(time.time())
                write_profile(outgoing)
            _backup(kind, live)

        backend.write_live(target["payload"])

        # Verify, and put the previous login back if the store didn't take.
        check = backend.read_live()
        got = _fingerprint(backend.identity(check)) if check else None
        if target.get("fingerprint") and got != target["fingerprint"]:
            if live is not None:
                try:
                    backend.write_live(live)
                except SwitchError:
                    pass
            raise SwitchError(
                f"{kind}: write did not take (store still reports a different "
                f"account); previous login restored"
            )

        target["last_used"] = int(time.time())
        write_profile(target)

    stamp_badges()
    ident = backend.describe(target.get("identity") or {})
    notify(f"{backend.title}: {target['label']}", ident)
    return f"{kind}: switched to {target['label']} ({ident})"


def next_profile(kind):
    profiles = list_profiles(kind)
    if not profiles:
        raise SwitchError(f"no saved {kind} profiles yet — run the save action first")
    if len(profiles) == 1:
        return switch(kind, profiles[0]["slug"])
    current = active_profile(kind)
    idx = 0
    if current:
        slugs = [p["slug"] for p in profiles]
        idx = (slugs.index(current["slug"]) + 1) % len(profiles)
    return switch(kind, profiles[idx]["slug"])


# ---- usage ----------------------------------------------------------------
#
# Both harnesses publish what is left of the current account's allowance. The
# numbers come from the account, so they hold whatever tool is spending it.
#
# Unofficial endpoints: they can change or vanish, so every read fails soft and
# the picker works without them.

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_USAGE_BETA = "oauth-2025-04-20"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
USAGE_CACHE = os.path.join(STATE_DIR, "usage-cache.json")
USAGE_TTL_S = _num_env("ACCOUNT_SWITCH_USAGE_TTL_S", 120.0)
USAGE_TIMEOUT_S = _num_env("ACCOUNT_SWITCH_USAGE_TIMEOUT_S", 8.0)
# How long to leave an account alone after it answers 429.
USAGE_BACKOFF_S = _num_env("ACCOUNT_SWITCH_USAGE_BACKOFF_S", 300.0)
BAR_WIDTH = int(_num_env("ACCOUNT_SWITCH_USAGE_BAR_WIDTH", 10))
_BAR_CHARS = _setting("ACCOUNT_SWITCH_USAGE_BAR") or "█░"
BAR_FULL = _BAR_CHARS[0]
BAR_EMPTY = _BAR_CHARS[1] if len(_BAR_CHARS) > 1 else " "


def _thresholds():
    raw = _setting("ACCOUNT_SWITCH_USAGE_THRESHOLDS") or "60,85"
    try:
        warn, crit = (float(x) for x in raw.split(",", 1))
        return warn, crit
    except ValueError:
        return 60.0, 85.0


WARN_AT, CRIT_AT = _thresholds()

# Named colours, resolved to curses constants when the overlay opens and to
# ANSI codes for the one-line form.
COLOR_NAMES = {
    "black": 0, "red": 1, "green": 2, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
}


def _color_map():
    out = {"ok": "green", "warn": "yellow", "crit": "red", "stale": "blue"}
    raw = _setting("ACCOUNT_SWITCH_USAGE_COLORS") or ""
    for part in raw.split(","):
        if "=" in part:
            key, value = part.split("=", 1)
            key, value = key.strip().lower(), value.strip().lower()
            if key in out and value in COLOR_NAMES:
                out[key] = value
    return out


USAGE_COLORS = _color_map()


def severity_of(percent):
    if percent is None:
        return "stale"
    if percent >= CRIT_AT:
        return "crit"
    if percent >= WARN_AT:
        return "warn"
    return "ok"


def bar_for(percent, width=None):
    width = width or BAR_WIDTH
    if percent is None:
        return BAR_EMPTY * width
    filled = int(round(max(0.0, min(100.0, percent)) / 100.0 * width))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def _http_json(url, headers):
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_S) as response:
        return json.load(response)


def _iso_epoch(text):
    if isinstance(text, (int, float)):
        return float(text)
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _jwt_expiry(token):
    """`exp` out of a JWT — only used to skip a token already known to be dead."""
    try:
        return float(_jwt_claims(token).get("exp"))
    except (TypeError, ValueError):
        return None


def _claude_auth(payload):
    """(token, expires_at) from a claude credential payload."""
    creds = (payload or {}).get("credentials")
    if isinstance(creds, str):
        try:
            creds = json.loads(creds)
        except ValueError:
            creds = None
    oauth = (creds or {}).get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    expires = oauth.get("expiresAt")
    if isinstance(expires, (int, float)) and expires > 10 ** 11:
        expires = expires / 1000.0  # milliseconds
    return token, expires


def _codex_auth(payload):
    """(token, account_id, expires_at) from a codex auth payload."""
    auth = (payload or {}).get("auth") or {}
    tokens = auth.get("tokens") or {}
    token = tokens.get("access_token")
    return token, tokens.get("account_id"), _jwt_expiry(token or "")


def _scoped_label(entry):
    """"weekly_scoped" says nothing; the scope names the model it applies to."""
    label = str(entry.get("kind") or entry.get("group") or "window")
    scope = entry.get("scope") or {}
    model = (scope.get("model") or {}).get("display_name")
    if not model:
        return label
    return "%s %s" % (label.replace("_scoped", ""), model)


def _claude_windows(body):
    out = []
    for entry in (body or {}).get("limits") or []:
        if not isinstance(entry, dict):
            continue
        out.append({
            "label": _scoped_label(entry),
            "percent": entry.get("percent"),
            "resets_at": _iso_epoch(entry.get("resets_at")),
            "blocked": None,
            # Which window is currently the one that would stop you.
            "binding": bool(entry.get("is_active")),
        })
    if out:
        return out
    for name in ("five_hour", "seven_day"):
        block = (body or {}).get(name)
        if isinstance(block, dict):
            out.append({
                "label": name,
                "percent": block.get("utilization"),
                "resets_at": _iso_epoch(block.get("resets_at")),
                "blocked": None,
                "binding": False,
            })
    return out


def _codex_windows(body):
    rate = (body or {}).get("rate_limit") or {}
    reached = rate.get("limit_reached")
    out = []
    for name in ("primary_window", "secondary_window"):
        window = rate.get(name)
        if not isinstance(window, dict):
            continue
        seconds = window.get("limit_window_seconds")
        hours = (seconds or 0) / 3600.0
        # Name it for what it is, so it reads the same as the claude rows.
        if hours >= 24 * 6:
            label = "weekly"
        elif hours >= 24:
            label = "%gd" % round(hours / 24.0, 1)
        elif hours:
            label = "%gh" % round(hours, 1)
        else:
            label = name.split("_")[0]
        resets = window.get("reset_at")
        if resets is None and window.get("reset_after_seconds") is not None:
            resets = time.time() + window["reset_after_seconds"]
        out.append({
            "label": label,
            "percent": window.get("used_percent"),
            "resets_at": _iso_epoch(resets),
            "seconds": seconds or None,
            # codex says outright whether it is blocked; nothing to infer.
            "blocked": bool(reached) if reached is not None else None,
            "binding": False,
        })
    return out


# ---- token renewal --------------------------------------------------------
#
# A parked profile's access token has usually expired, so its usage cannot be
# read without renewing it first. Renewing SPENDS the stored refresh token: the
# reply carries a fresh pair and the old one dies. Lose that reply and the
# account needs a browser login, so the new pair is written into the profile
# before it is used for anything else. This mirrors what the CLIs themselves do
# on every expiry, and what openusage does to keep its panel current.

CLAUDE_RENEW_URL = "https://platform.claude.com/v1/oauth/token"
CLAUDE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CODEX_RENEW_URL = "https://auth.openai.com/oauth/token"
CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
# Renew this far ahead of expiry rather than after it, so a token cannot die
# mid-read. openusage uses the same five minutes.
RENEW_MARGIN_S = _num_env("ACCOUNT_SWITCH_RENEW_MARGIN_S", 300.0)
USAGE_RENEW = _flag("ACCOUNT_SWITCH_USAGE_RENEW", default=True)
# Ask the provider whether a saved login still works before installing it.
VERIFY_SWITCH = _flag("ACCOUNT_SWITCH_VERIFY_SWITCH", default=True)


# The token endpoint answers the CLI's own HTTP client, and refuses a default
# urllib User-Agent, so the request is shaped like the one the CLI makes.
TOKEN_USER_AGENT = "axios/1.15.2"
TOKEN_ACCEPT = "application/json, text/plain, */*"
# The endpoint is order-sensitive about scopes; this is the order the CLI sends.
CANONICAL_SCOPES = [
    "org:create_api_key", "user:profile", "user:inference",
    "user:sessions:claude_code", "user:mcp_servers", "user:file_upload",
]


def _canonical_scopes(scopes):
    present = [s for s in (scopes or []) if s]
    ordered = [s for s in CANONICAL_SCOPES if s in present]
    ordered += [s for s in present if s not in CANONICAL_SCOPES]
    return " ".join(ordered)


def _post_json(url, body):
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": TOKEN_ACCEPT,
            "User-Agent": TOKEN_USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=USAGE_TIMEOUT_S) as response:
        return json.load(response)


def _renewed_claude(oauth):
    """A renewed claudeAiOauth block, or None. Spends the stored refresh token."""
    stored = (oauth or {}).get("refreshToken")
    if not stored:
        return None
    scopes = oauth.get("scopes") or []
    body = _post_json(CLAUDE_RENEW_URL, {
        "grant_type": "refresh_token",
        "refresh_token": stored,
        "client_id": CLAUDE_CLIENT_ID,
        "scope": _canonical_scopes(scopes) or _canonical_scopes(CANONICAL_SCOPES[1:]),
    })
    access = body.get("access_token")
    if not access:
        return None
    out = dict(oauth)
    out["accessToken"] = access
    # The reply rotates the refresh token. Keep the old one only when none came
    # back, because dropping a live one strands the account.
    if body.get("refresh_token"):
        out["refreshToken"] = body["refresh_token"]
    if body.get("expires_in"):
        out["expiresAt"] = int((time.time() + float(body["expires_in"])) * 1000)
    return out


def _renewed_codex(tokens):
    """A renewed codex tokens block, or None. Spends the stored refresh token."""
    stored = (tokens or {}).get("refresh_token")
    if not stored:
        return None
    body = _post_json(CODEX_RENEW_URL, {
        "grant_type": "refresh_token",
        "refresh_token": stored,
        "client_id": CODEX_CLIENT_ID,
    })
    access = body.get("access_token")
    if not access:
        return None
    out = dict(tokens)
    out["access_token"] = access
    if body.get("refresh_token"):
        out["refresh_token"] = body["refresh_token"]
    if body.get("id_token"):
        out["id_token"] = body["id_token"]
    return out


def _due_for_renewal(expires_at):
    return expires_at is not None and expires_at - time.time() <= RENEW_MARGIN_S


STAGED_DIR = os.path.join(STATE_DIR, "rotated")


def _staged_path(kind, slug):
    return os.path.join(STAGED_DIR, "%s-%s.json" % (kind, slug))


def _stage(kind, slug, payload):
    """Park a rotated payload on disk the instant it exists.

    Between the endpoint answering and the profile being rewritten, the reply is
    the only usable pair that account has. Writing it here first means a crash
    in that window costs a stale profile, not a login.
    """
    _secure_dir(STAGED_DIR)
    _write_json_secret(_staged_path(kind, slug), payload)


def _adopt_staged(kind, slug, payload):
    """Take up a payload staged by a run that died before it could finish."""
    staged = _read_json(_staged_path(kind, slug))
    return staged if staged else payload


def _clear_staged(kind, slug):
    try:
        os.remove(_staged_path(kind, slug))
    except OSError:
        pass


def renew_profile(kind, profile, force=False):
    """Renew one parked profile's tokens in place; returns its payload or None.

    Ordering is the whole point. Back up before going near the network, stage
    the reply the moment it lands, then rewrite the profile. From the moment the
    endpoint answers, the old refresh token is dead and the reply is the only
    usable pair that account has.

    `force` renews a token the clock still calls valid. Both providers can
    supersede a session — signing into another account on the same provider does
    it — and the old access token keeps claiming a future expiry regardless. The
    server is the authority, so a 401 asks for this.
    """
    with _Lock():
        current = get_profile(kind, profile["slug"]) or profile
        slug = current["slug"]
        payload = _adopt_staged(kind, slug, current.get("payload") or {})
        if kind == "claude":
            creds = payload.get("credentials")
            if isinstance(creds, str):
                try:
                    creds = json.loads(creds)
                except ValueError:
                    return None
            if not force and not _due_for_renewal(_claude_auth(payload)[1]):
                return payload
            _backup(kind, payload)
            renewed = _renewed_claude((creds or {}).get("claudeAiOauth") or {})
            if not renewed:
                return None
            creds = dict(creds or {})
            creds["claudeAiOauth"] = renewed
            payload = dict(payload)
            payload["credentials"] = creds
        elif kind == "codex":
            auth = dict(payload.get("auth") or {})
            tokens = auth.get("tokens") or {}
            if not force and not _due_for_renewal(
                    _jwt_expiry(tokens.get("access_token") or "")):
                return payload
            _backup(kind, payload)
            renewed = _renewed_codex(tokens)
            if not renewed:
                return None
            auth["tokens"] = renewed
            auth["last_refresh"] = datetime.now().isoformat()
            payload = dict(payload)
            payload["auth"] = auth
        else:
            return None
        _stage(kind, slug, payload)
        current = dict(current)
        current["payload"] = payload
        write_profile(current)
        _clear_staged(kind, slug)
    return payload


def fetch_usage(kind, payload):
    """Windows for one account, or None when it cannot be read."""
    if kind == "claude":
        token, expires = _claude_auth(payload)
        if not token or (expires and expires < time.time()):
            return None
        return _claude_windows(_http_json(CLAUDE_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "anthropic-beta": CLAUDE_USAGE_BETA,
        }))
    if kind == "codex":
        token, account_id, expires = _codex_auth(payload)
        if not token or (expires and expires < time.time()):
            return None
        return _codex_windows(_http_json(CODEX_USAGE_URL, {
            "Authorization": "Bearer %s" % token,
            "chatgpt-account-id": account_id or "",
            "User-Agent": "codex-cli",
            "Accept": "application/json",
        }))
    return None


def _refresh_refused(exc):
    """True when a token endpoint turned a refresh token down for good.

    A rotated refresh token is spent, and the reply says so in words:
    `invalid_grant`, or the reuse error a provider raises when the same one
    comes back twice. A 401 there is the same answer. Anything vaguer than
    that is not proof that the account is dead.
    """
    if exc.code not in (400, 401):
        return False
    if exc.code == 401:
        return True
    try:
        body = json.loads(exc.read().decode() or "{}")
    except Exception:
        return False
    said = " ".join(
        str(body.get(field) or "")
        for field in ("error", "error_description", "detail")
    ).lower()
    return "invalid_grant" in said or "reused" in said


def proven_payload(kind, profile):
    """The profile's payload, proven still usable, renewed if it had to be.

    switch() checks that the credential store took the write. That says nothing
    about whether the login still works, and the two come apart exactly when it
    matters: a snapshot the provider has since retired installs cleanly,
    reports success, and signs you out. So ask the provider first, while the
    working login is still in place.

    Only a definitive refusal stops a switch — the provider answering 401 to a
    token it was just handed, or turning the refresh token down in words. Being
    offline, timing out, or getting rate limited proves nothing either way, and
    none of them block the switch.

    Returns the payload to install. It is not always the one that came in:
    renewing rotates the tokens, and the caller must write *this* one back, or
    it parks a spent pair over a live one.
    """
    payload = profile.get("payload") or {}
    if not VERIFY_SWITCH:
        return payload
    label = profile.get("label") or profile.get("slug") or kind
    try:
        payload = renew_profile(kind, profile) or payload
        try:
            fetch_usage(kind, payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 401:
                raise
            # 401 on a token the clock still calls valid: the session was
            # superseded. Renew past the clock and ask once more, because the
            # server is the authority and the expiry is only a claim.
            renewed = renew_profile(kind, profile, force=True)
            if not renewed:
                raise
            payload = renewed
            fetch_usage(kind, payload)
    except urllib.error.HTTPError as exc:
        if exc.code == 401 or _refresh_refused(exc):
            raise SwitchError(
                "%s: %s was refused — nothing changed; log that account in "
                "again and save it" % (kind, label)
            )
        return payload
    except Exception:
        return payload
    return payload


def _usage_cache():
    return _read_json(USAGE_CACHE) or {}


def _remember_usage(key, windows):
    cache = _usage_cache()
    entry = cache.get(key) or {}
    entry.update({"at": time.time(), "windows": windows})
    entry.pop("retry_after", None)
    cache[key] = entry
    _secure_dir(STATE_DIR)
    _write_json_secret(USAGE_CACHE, cache)


def _rest_usage(key, seconds):
    """Stop asking for a while. Keeps whatever windows were last read."""
    cache = _usage_cache()
    entry = cache.get(key) or {}
    entry["retry_after"] = time.time() + seconds
    cache[key] = entry
    _secure_dir(STATE_DIR)
    _write_json_secret(USAGE_CACHE, cache)


def usage_rows(kinds=None, refresh=True):
    """One row per saved profile: its windows, and how current they are.

    A parked profile usually holds an expired token, so its numbers come from
    the last time it was live. That is said out loud rather than hidden: the
    row carries `age`, and `state` is live, cached or unknown.
    """
    rows = []
    cache = _usage_cache()
    for kind in kinds or KIND_ORDER:
        backend = BACKENDS[kind]
        live = backend.read_live()
        live_fp = _fingerprint(backend.identity(live)) if live else None
        for profile in list_profiles(kind):
            key = "%s:%s" % (kind, profile["slug"])
            is_live = bool(live_fp) and profile.get("fingerprint") == live_fp
            payload = live if is_live else profile.get("payload")
            windows, state, problem = None, "unknown", None
            entry = cache.get(key) or {}
            # The endpoint rate-limits, so the live account observes the cache
            # too. It used to refetch on every call, which is what earns a 429.
            stale = (time.time() - (entry.get("at") or 0)) > USAGE_TTL_S
            resting = time.time() < (entry.get("retry_after") or 0)
            if refresh and stale and not resting:
                try:
                    # A parked account's token has usually expired, so renewing
                    # is what makes its numbers readable at all. The live one is
                    # kept fresh by the CLI, so that chain is left alone.
                    if USAGE_RENEW and not is_live:
                        payload = renew_profile(kind, profile) or payload
                    try:
                        windows = fetch_usage(kind, payload)
                    except urllib.error.HTTPError as exc:
                        # 401 on a token the clock still calls valid: the session
                        # was superseded, which signing into another account on
                        # the same provider does. Renew past the clock and retry
                        # once — the server is the authority, not the expiry.
                        if exc.code != 401 or is_live or not USAGE_RENEW:
                            raise
                        payload = renew_profile(kind, profile, force=True)
                        if not payload:
                            raise
                        windows = fetch_usage(kind, payload)
                except Exception as exc:
                    # Say why, briefly: this shares a narrow column with the
                    # account name, and a truncated traceback tells nobody
                    # anything.
                    code = getattr(exc, "code", None)
                    problem = {401: "needs re-login", 403: "refused",
                               404: "no usage endpoint"}.get(code)
                    if code == 429:
                        # Asked too often. Sit out, or every later call compounds it.
                        problem = "rate limited"
                        _rest_usage(key, USAGE_BACKOFF_S)
                    elif problem is None:
                        problem = "unreachable" if isinstance(
                            exc, urllib.error.URLError) else type(exc).__name__
                    windows = None
            if windows:
                state = "live"
                _remember_usage(key, windows)
                at = time.time()
            elif entry.get("windows"):
                windows, state, at = entry["windows"], "cached", entry.get("at")
            else:
                windows, at = [], None
            rows.append({
                "kind": kind,
                "slug": profile["slug"],
                "label": profile["label"],
                "active": is_live,
                "state": state,
                "at": at,
                "age": (time.time() - at) if at else None,
                "windows": windows,
                "problem": problem,
            })
    return rows


def _left(seconds):
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds <= 0:
        return "now"
    days, rest = divmod(seconds, 86400)
    hours, minutes = divmod(rest // 60, 60)
    if days:
        return "%dd%02dh" % (days, hours)
    if hours:
        return "%dh%02d" % (hours, minutes)
    return "%dm" % minutes


def _age(seconds):
    if seconds is None:
        return "never read"
    if seconds < 60:
        return "just now"
    return "%s ago" % _left(seconds)


# ---- badges ---------------------------------------------------------------

def account_labels(kinds=None):
    """{kind: label} for the kinds worth naming. One saved profile is enough.

    Saving a profile is the signal that you care which account is in use, and
    the badge earns its space straight away: with one profile it still says
    whether the live login is that profile or something else.
    """
    labels = {}
    for kind in kinds or KIND_ORDER:
        if not list_profiles(kind) and not BADGE_ALWAYS:
            continue
        backend = BACKENDS[kind]
        live = backend.read_live()
        if live is None:
            labels[kind] = "logged out"
            continue
        current = active_profile(kind, live)
        if current:
            labels[kind] = current["label"]
        else:
            # Logged into something no profile has a copy of. Name it from the
            # live identity and mark it, rather than showing a bare "?".
            labels[kind] = (
                default_label(kind, backend.identity(live) or {}) + UNSAVED_MARK
            )
    return labels


def _format_badge(name):
    return BADGE_FORMAT.format(glyph=GLYPH, name=name)


def _set_badge(pane_id, text):
    herdr(
        "pane", "report-metadata", pane_id,
        "--source", PLUGIN_ID,
        "--token", f"{TOKEN}={text}",
    )


def _clear_badge(pane_id):
    herdr(
        "pane", "report-metadata", pane_id,
        "--source", PLUGIN_ID,
        "--clear-token", TOKEN,
    )


def stamp_badges():
    """Label every claude/codex pane with the account it is currently using.

    Credentials are machine-wide per kind, so this is the same value on every
    pane of that kind — it is there so you can see, at a glance in the sidebar,
    which account the fleet is burning. Panes opened later get stamped on the
    next switch, startup, or picker refresh.
    """
    if not BADGE:
        return
    agents = live_agents()
    if agents is None:
        return
    labels = account_labels()
    for pane_id, info in agents.items():
        kind = kind_of_agent(info)
        if kind is None:
            continue
        if kind in labels:
            _set_badge(pane_id, _format_badge(labels[kind]))
        else:
            _clear_badge(pane_id)


# ---- kind resolution ------------------------------------------------------

def _context_pane():
    for env in ("HERDR_ACTIVE_PANE_ID", "HERDR_PANE_ID"):
        if os.environ.get(env):
            return os.environ[env]
    raw = os.environ.get("HERDR_PLUGIN_CONTEXT_JSON")
    if raw:
        try:
            ctx = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return ctx.get("focused_pane_id") or ctx.get("pane_id")
    return None


def resolve_kind(explicit=None):
    """Which agent kind an argument-less action should act on: the focused
    agent's kind, else the one kind that actually has profiles to cycle."""
    if explicit:
        if explicit not in BACKENDS:
            raise SwitchError(f"unknown agent kind {explicit!r} (claude|codex)")
        return explicit
    agents = live_agents() or {}
    pane = _context_pane()
    if pane and pane in agents:
        kind = kind_of_agent(agents[pane])
        if kind:
            return kind
    for info in agents.values():
        if info.get("focused"):
            kind = kind_of_agent(info)
            if kind:
                return kind
    switchable = [k for k in KIND_ORDER if len(list_profiles(k)) > 1]
    if len(switchable) == 1:
        return switchable[0]
    saved = [k for k in KIND_ORDER if list_profiles(k)]
    if len(saved) == 1:
        return saved[0]
    raise SwitchError(
        "can't tell which agent to switch — focus a claude/codex pane, or "
        "pass the kind (switcher.py next claude)"
    )


# ---- commands -------------------------------------------------------------

def cmd_open(argv):
    stamp_badges()
    res = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "picker",
        "--placement", "overlay",
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr or "account-switch: failed to open picker\n")
    return res.returncode


def cmd_open_usage(argv):
    res = herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", "usage",
        "--placement", "overlay",
    )
    if res.returncode != 0:
        sys.stderr.write(res.stderr or "account-switch: failed to open usage\n")
    return res.returncode


def cmd_next(argv):
    kind = resolve_kind(argv[0] if argv else None)
    print(next_profile(kind))
    return 0


def cmd_switch(argv):
    if len(argv) < 2:
        raise SwitchError("usage: switcher.py switch <claude|codex> <label>")
    kind = resolve_kind(argv[0])
    print(switch(kind, argv[1]))
    return 0


def save_live(kind, label=None):
    """Snapshot the live login of one kind. Returns a status string."""
    backend = BACKENDS[kind]
    with _Lock():
        live = backend.read_live()
        if live is None:
            raise SwitchError(f"{kind}: nothing logged in to save")
        identity = backend.identity(live)
        fp = _fingerprint(identity)
        existing = next(
            (p for p in list_profiles(kind) if fp and p.get("fingerprint") == fp), None
        )
        if existing:
            return (f"{kind}: already saved as {existing['label']} "
                    f"(rename it with r in the picker)")
        profile = make_profile(kind, label or default_label(kind, identity), live)
        write_profile(profile)
    stamp_badges()
    return f"{kind}: saved {profile['label']} ({backend.describe(profile['identity'])})"


def cmd_save(argv):
    if argv:
        kind = resolve_kind(argv[0])
        print(save_live(kind, argv[1] if len(argv) > 1 else None))
        return 0
    saved_any = False
    for kind in KIND_ORDER:
        if not BACKENDS[kind].present():
            continue
        try:
            print(save_live(kind))
            saved_any = True
        except SwitchError as exc:
            print(exc, file=sys.stderr)
    if not saved_any:
        return 1
    return 0


def cmd_status(argv):
    for kind in KIND_ORDER:
        backend = BACKENDS[kind]
        profiles = list_profiles(kind)
        if not backend.present() and not profiles:
            continue
        live = backend.read_live()
        if live is None:
            current = "not logged in"
        else:
            active = active_profile(kind, live)
            desc = backend.describe(backend.identity(live))
            current = f"{active['label']} ({desc})" if active else f"unsaved ({desc})"
        print(f"{backend.title:<12} {current}")
        for p in profiles:
            mark = "*" if live is not None and p.get("fingerprint") == _fingerprint(
                backend.identity(live)
            ) else " "
            print(f"  {mark} {p['label']:<16} {backend.describe(p.get('identity') or {})}")
        if not profiles:
            print("    (no profiles saved — run the save action)")
    return 0


def cmd_list(argv):
    """Profiles as JSON, for another plugin to read. Never prints credentials."""
    kinds = KIND_ORDER
    if "--kind" in argv:
        wanted = argv[argv.index("--kind") + 1]
        kinds = [wanted] if wanted in BACKENDS else []
    out = []
    for kind in kinds:
        live = BACKENDS[kind].read_live()
        live_fp = _fingerprint(BACKENDS[kind].identity(live)) if live else None
        for profile in list_profiles(kind):
            out.append({
                "kind": kind,
                "slug": profile["slug"],
                "label": profile["label"],
                "active": bool(live_fp) and profile.get("fingerprint") == live_fp,
                "saved_at": profile.get("saved_at"),
                "last_used": profile.get("last_used"),
                "tier": (profile.get("identity") or {}).get("subscription_type"),
            })
    print(json.dumps(out))
    return 0


def _usage_pairs(curses):
    """Severity name -> curses attribute, from the configured palette."""
    pairs = {}
    if not curses.has_colors():
        return pairs
    curses.start_color()
    try:
        curses.use_default_colors()
    except curses.error:
        pass
    for index, (name, colour) in enumerate(USAGE_COLORS.items(), start=1):
        try:
            curses.init_pair(index, COLOR_NAMES[colour], -1)
            pairs[name] = curses.color_pair(index)
        except curses.error:
            pairs[name] = 0
    return pairs


def cmd_usage_ui(argv):
    """Coloured usage panel: every saved profile, every window it reports."""
    import curses

    def run(stdscr):
        curses.curs_set(0)
        pairs = _usage_pairs(curses)
        # Reading two accounts is a network round trip, so refresh on a timer
        # rather than on every keypress.
        stdscr.timeout(2000)
        rows, fetched = usage_rows(), time.time()
        while True:
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            stdscr.addnstr(0, 0, "USAGE", width - 1, curses.A_BOLD)
            stdscr.addnstr(
                1, 0, "r refresh · q quit · read %s"
                % _age(time.time() - fetched), width - 1, curses.A_DIM,
            )
            y = 3
            for kind in KIND_ORDER:
                mine = [r for r in rows if r["kind"] == kind]
                if not mine or y >= height - 1:
                    continue
                stdscr.addnstr(y, 0, BACKENDS[kind].title, width - 1,
                               curses.A_BOLD | curses.A_UNDERLINE)
                y += 1
                for row in mine:
                    if y >= height - 1:
                        break
                    head = "%s %s" % ("●" if row["active"] else " ", row["label"])
                    if row["state"] != "live":
                        head += "   %s" % _age(row["age"])
                    stdscr.addnstr(y, 2, head, width - 3,
                                   curses.A_BOLD if row["active"] else curses.A_DIM)
                    y += 1
                    if not row["windows"]:
                        stdscr.addnstr(y, 6, "never read", width - 7, curses.A_DIM)
                        y += 1
                    for window in row["windows"]:
                        if y >= height - 1:
                            break
                        percent = window.get("percent")
                        live = row["state"] == "live"
                        sev = severity_of(percent if live else None)
                        resets = window.get("resets_at")
                        left = _left(resets - time.time()) if resets else "—"
                        attr = pairs.get(sev, 0) | (0 if live else curses.A_DIM)
                        stdscr.addnstr(
                            y, 6,
                            "%-14s %s %4s%%  %s" % (
                                window.get("label", "window"),
                                bar_for(percent),
                                "?" if percent is None else round(percent),
                                left,
                            ),
                            width - 7, attr,
                        )
                        y += 1
                y += 1
            stdscr.refresh()
            try:
                key = stdscr.getch()
            except KeyboardInterrupt:
                return
            if key in (ord("q"), 27):
                return
            if key == ord("r"):
                rows, fetched = usage_rows(), time.time()

    curses.wrapper(run)
    return 0


ANSI_OFF = "\033[0m"


def _ansi(text, severity, enabled):
    """Colour by the same palette the overlay uses, so both agree."""
    if not enabled:
        return text
    name = USAGE_COLORS.get(severity)
    if name is None:
        return text
    return "\033[%dm%s%s" % (30 + COLOR_NAMES[name], text, ANSI_OFF)


def cmd_usage(argv):
    """What is left on every saved account, for both harnesses."""
    if "--json" in argv:
        print(json.dumps(usage_rows(), indent=2))
        return 0
    color = "--color" in argv
    rows = usage_rows()
    if not rows:
        print("no profiles saved yet — run the save action")
        return 1
    for kind in KIND_ORDER:
        mine = [r for r in rows if r["kind"] == kind]
        if not mine:
            continue
        print(BACKENDS[kind].title)
        for row in mine:
            mark = "●" if row["active"] else " "
            note = "" if row["state"] == "live" else "  (%s)" % _age(row["age"])
            print("  %s %-14s%s" % (mark, row["label"], note))
            if not row["windows"]:
                print("      %s" % (row.get("problem") or
                                    ("never read" if row["state"] == "unknown"
                                     else "no windows reported")))
            for window in row["windows"]:
                percent = window.get("percent")
                sev = severity_of(percent if row["state"] == "live" else None)
                left = window.get("resets_at")
                left = _left(left - time.time()) if left else "—"
                text = "%-14s %s %5s%%  resets %s" % (
                    window.get("label", "window"),
                    bar_for(percent),
                    "?" if percent is None else round(percent),
                    left,
                )
                print("      " + _ansi(text, sev, color))
    return 0


def refresh_stable_link():
    """Point STATE_DIR/current at wherever this copy of the plugin lives.

    Replaced atomically, so a `tab_bar_right` or status-line command reading
    through it never sees a missing path mid-update.
    """
    try:
        # A link to itself is unreadable and unrecoverable from the outside,
        # so refuse it outright rather than trusting the caller's path.
        if os.path.abspath(STABLE_LINK) == ROOT:
            return None
        if os.path.realpath(STABLE_LINK) == ROOT:
            return STABLE_LINK
        _secure_dir(STATE_DIR)
        tmp = STABLE_LINK + ".tmp"
        if os.path.islink(tmp) or os.path.exists(tmp):
            os.remove(tmp)
        os.symlink(ROOT, tmp)
        os.replace(tmp, STABLE_LINK)
        return STABLE_LINK
    except OSError:
        return None  # a badge is not worth failing a startup hook over


def cmd_stamp(argv):
    refresh_stable_link()
    stamp_badges()
    warn_if_unwired()
    return 0


def cmd_badge(argv):
    """One line naming the live account(s), for `tab_bar_right`.

    Credentials are machine-wide per kind, so the account is the same on every
    pane. This prints it once for the tab bar instead of stamping every pane.
    Writes nothing and talks to no socket: herdr re-runs it on its own interval.
    """
    kinds = [k for k in argv if k in BACKENDS] or None
    labels = account_labels(kinds)
    if not labels:
        return 0
    named = len(labels) > 1
    print(SEPARATOR.join(
        (f"{kind} " if named else "") + _format_badge(label)
        for kind, label in labels.items()
    ))
    return 0


# ---- overlay picker -------------------------------------------------------

def _rows():
    """Display model: ("head", kind, text) / ("profile", kind, profile) /
    ("empty", kind, text). Only "profile" rows are selectable."""
    rows = []
    for kind in KIND_ORDER:
        backend = BACKENDS[kind]
        profiles = list_profiles(kind)
        if not backend.present() and not profiles:
            continue
        live = backend.read_live()
        live_fp = _fingerprint(backend.identity(live)) if live else None
        head = backend.title
        if live is None:
            head += "  — not logged in"
        elif not any(p.get("fingerprint") == live_fp for p in profiles):
            head += f"  — live: {backend.describe(backend.identity(live))} (unsaved, press s)"
        rows.append(("head", kind, head))
        for p in profiles:
            p["_active"] = bool(live_fp) and p.get("fingerprint") == live_fp
            rows.append(("profile", kind, p))
        if not profiles:
            rows.append(("empty", kind, "  (nothing saved — press s to save the current login)"))
    if not rows:
        rows.append(("empty", None, "  no Claude Code or Codex install found"))
    return rows


LABEL_W = 16
USAGE_W = 20
# Where the usage column starts: " " + mark + " " + label + " ".
USAGE_COL = 3 + LABEL_W + 1


def _profile_line(kind, p, width, usage=None):
    backend = BACKENDS[kind]
    mark = "●" if p.get("_active") else " "  # ●
    desc = backend.describe(p.get("identity") or {})
    label = str(p["label"])[:LABEL_W]
    if usage is None:
        line = " %s %-*s %s" % (mark, LABEL_W, label, desc)
    else:
        line = " %s %-*s %-*s %s" % (
            mark, LABEL_W, label, USAGE_W, usage[:USAGE_W], desc)
    return line[: max(0, width - 1)]


def binding_window(row):
    """The window closest to stopping you: the fullest one, then the soonest.

    The API marks one `is_active`; that is preferred when it is there, since it
    is the account's own answer rather than a guess from the numbers.
    """
    windows = [w for w in shown_windows(row)
               if isinstance(w.get("percent"), (int, float))]
    if not windows:
        return None
    marked = [w for w in windows if w.get("binding")]
    pool = marked or windows
    return max(pool, key=lambda w: (w["percent"], -(w.get("resets_at") or 0)))


FIVE_HOURS = 5 * 3600
SEVEN_DAYS = 7 * 86400

def _centred(text, width):
    """A column title sitting over the middle of its column."""
    if width <= len(text):
        return text[:width]
    pad = width - len(text)
    return " " * (pad // 2) + text + " " * (pad - pad // 2)


# One row per window, and the header built from the same widths so the two
# cannot drift apart.
DETAIL_FMT = "%-9s %3s%%  %s  %s"
DETAIL_HEAD = "%s %s  %s  %s" % (
    _centred("window", 9), _centred("used", 4), _centred("", 10), "at this rate")
# The collapsed view's one-line summary: bar, percent, time to reset.
SUMMARY_FMT = "%s %3d%% %s"
SUMMARY_HEAD = "%s %s %s" % (
    _centred("", 8), _centred("used", 4), "resets in")


def window_seconds(window):
    """How long this window runs, so elapsed time can be worked out."""
    if window.get("seconds"):
        return window["seconds"]
    label = (window.get("label") or "").lower()
    if label in ("session", "five_hour"):
        return FIVE_HOURS
    if label.startswith("weekly") or label.startswith("seven_day"):
        return SEVEN_DAYS
    # A duration used as a name, e.g. codex's "7d" / "168h" from an older cache.
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([dh])", label)
    if match:
        return float(match.group(1)) * (86400 if match.group(2) == "d" else 3600)
    return None


def window_name(label):
    """The short name to show, rather than the API's key."""
    text = str(label or "")
    lowered = text.lower()
    if lowered in ("session", "five_hour"):
        return "Session"
    if lowered in ("weekly_all", "seven_day", "weekly"):
        return "Weekly"
    if lowered.startswith("weekly "):
        return text.split(" ", 1)[1]        # the model's own name, e.g. Fable
    return text


def projection(window):
    """Where this window lands by its reset, at the rate it is being spent.

    This is the number that decides whether to switch accounts now: 66% used is
    fine with a day left and a problem with five. The reset comes along with it,
    because "limit in 1d12h" means nothing without knowing what it is racing.
    """
    percent, resets = window.get("percent"), window.get("resets_at")
    if not resets:
        return "—"
    until = _left(resets - time.time())
    total = window_seconds(window)
    left = resets - time.time()
    elapsed = (total - left) if total else None
    if percent is not None and percent >= 100:
        return "spent (resets in %s)" % until
    # A window minutes old gives a rate wild enough to claim the limit is
    # imminent, so say nothing until enough of it has run to mean something.
    settled = elapsed is not None and elapsed >= max(600.0, (total or 0) * 0.05)
    if percent is None or not settled or percent <= 0 or left <= 0:
        return "resets in %s" % until
    rate = percent / elapsed
    landing = percent + rate * left
    if landing < 100:
        return "~%d%% left (resets in %s)" % (round(100 - landing), until)
    return "limit in %s (resets in %s)" % (_left((100 - percent) / rate), until)


def shown_windows(row):
    """Codex reports one long window worth showing; claude reports three."""
    windows = list((row or {}).get("windows") or [])
    if (row or {}).get("kind") == "codex" and len(windows) > 1:
        return [max(windows, key=lambda w: window_seconds(w) or 0)]
    return windows


def usage_detail(row):
    """[(text, severity)] — one line per window, for the expanded view."""
    if not row:
        return []
    live = row.get("state") == "live"
    out = []
    for window in shown_windows(row):
        percent = window.get("percent")
        out.append((
            DETAIL_FMT % (
                window_name(window.get("label")),
                "?" if percent is None else round(percent),
                bar_for(percent, 10),
                projection(window),
            ),
            severity_of(percent if live else None),
        ))
    if not out:
        out.append(((row.get("problem") or "no windows read"), "stale"))
    return out


def usage_summary(row):
    """"How much is left" for one profile row, sized for the picker column.

    Only the binding window: which window that is, and the rest of them, is
    what the usage panel is for.
    """
    if row is None:
        return "", "stale"
    if row.get("state") == "unknown":
        return (row.get("problem") or "no usage read")[:USAGE_W], "stale"
    window = binding_window(row)
    if not window:
        return "no windows", "stale"
    live = row.get("state") == "live"
    percent = window["percent"]
    resets = window.get("resets_at")
    left = _left(resets - time.time()) if resets else "—"
    text = SUMMARY_FMT % (bar_for(percent, 8), round(percent), left)
    return text[:USAGE_W], severity_of(percent if live else None)


def _center_win(stdscr, height, width):
    import curses

    h, w = stdscr.getmaxyx()
    height, width = min(height, h), min(width, w)
    win = curses.newwin(
        height, width, max(0, (h - height) // 2), max(0, (w - width) // 2)
    )
    win.keypad(True)
    return win


def _ask(stdscr, title, question, initial=""):
    """Centred one-field dialog. Returns the text, or None on esc."""
    import curses

    hint = "enter confirm · esc cancel"
    buf = list(initial)
    width = min(
        max(len(question), len(title), len(hint), 44) + 6, stdscr.getmaxyx()[1]
    )
    try:
        while True:
            win = _center_win(stdscr, 8, width)
            win.erase()
            win.box()
            inner = width - 4
            win.addnstr(0, 2, " %s " % title, inner, curses.A_BOLD)
            win.addnstr(2, 2, question, inner, curses.A_BOLD)
            win.addnstr(4, 2, "> " + "".join(buf) + "_", inner)
            win.addnstr(6, 2, hint, inner, curses.A_DIM)
            win.refresh()
            ch = win.getch()
            if ch == 27:
                return None
            if ch in (curses.KEY_ENTER, 10, 13):
                return "".join(buf).strip()
            if ch in (curses.KEY_BACKSPACE, 127, 8):
                if buf:
                    buf.pop()
            elif 32 <= ch < 127:
                buf.append(chr(ch))
    finally:
        stdscr.touchwin()
        stdscr.refresh()


def _confirm(stdscr, title, question, note=None):
    """Centred yes/no dialog. Only `y` confirms; anything else cancels."""
    import curses

    hint = "y confirm · any other key cancel"
    width = min(
        max(len(question), len(title), len(hint), len(note or ""), 44) + 6,
        stdscr.getmaxyx()[1],
    )
    height = 9 if note else 7
    try:
        win = _center_win(stdscr, height, width)
        win.erase()
        win.box()
        inner = width - 4
        win.addnstr(0, 2, " %s " % title, inner, curses.A_BOLD)
        win.addnstr(2, 2, question, inner, curses.A_BOLD)
        if note:
            win.addnstr(4, 2, note, inner, curses.A_DIM)
        win.addnstr(height - 2, 2, hint, inner, curses.A_DIM)
        win.refresh()
        return win.getch() in (ord("y"), ord("Y"))
    finally:
        stdscr.touchwin()
        stdscr.refresh()


def cmd_ui(argv):
    import curses

    def run(stdscr):
        curses.curs_set(0)
        pairs = _usage_pairs(curses)
        # Slow refresh on purpose: each tick re-reads the credential store, and
        # on macOS that is a Keychain lookup per agent kind.
        stdscr.timeout(5000)
        sel = 0
        message = ""
        details = False
        # Read once on open, then from cache: the endpoint rate-limits, and the
        # picker's own tick is far faster than usage changes.
        usage = {"%s:%s" % (r["kind"], r["slug"]): r for r in usage_rows()}
        while True:
            rows = _rows()
            pickable = [
                i for i, r in enumerate(rows)
                if r[0] == "profile" or (r[0] == "empty" and r[1])
            ]
            if pickable:
                sel = min(max(sel, 0), len(pickable) - 1)
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            stdscr.addnstr(0, 0, "ACCOUNTS", w - 1, curses.A_BOLD)
            if h > 1:
                # Padded: the text shortens when details are on, and addnstr
                # leaves whatever the longer version wrote behind it.
                stdscr.addnstr(
                    1, 0,
                    ("j/k select · enter switch · s save · r rename · x delete · "
                     "d %s · u reread · q quit"
                     % ("hide" if details else "details")).ljust(w - 1),
                    w - 1, curses.A_DIM,
                )
            # Column titles, so the numbers are not left to be guessed at.
            if h > 2:
                # Three spaces first: the profile line spends them on " ● ".
                head = "   %s %s" % (
                    _centred("account", LABEL_W),
                    DETAIL_HEAD if details else SUMMARY_HEAD,
                )
                stdscr.addnstr(2, 0, head.ljust(w - 1), w - 1,
                               curses.A_DIM | curses.A_UNDERLINE)
            y = 4
            for i, (kind_row, kind, body) in enumerate(rows):
                if y >= h - 1:
                    break
                if kind_row == "head":
                    stdscr.addnstr(y, 0, body, w - 1, curses.A_BOLD | curses.A_UNDERLINE)
                elif kind_row == "empty":
                    is_sel = pickable and i == pickable[sel]
                    attr = curses.A_REVERSE if is_sel else curses.A_DIM
                    stdscr.addnstr(y, 0, body.ljust(w - 1), w - 1, attr)
                else:
                    is_sel = pickable and i == pickable[sel]
                    row = usage.get("%s:%s" % (kind, body["slug"]))
                    # Expanded, the windows below carry the numbers, so the
                    # account line would only repeat one of them.
                    text, sev = ("", "ok") if details else usage_summary(row)
                    line = _profile_line(kind, body, w, text).ljust(w - 1)
                    stdscr.addnstr(y, 0, line, w - 1,
                                   curses.A_REVERSE if is_sel else curses.A_NORMAL)
                    # Repaint just the usage column in its severity colour, so
                    # the palette reads as "how full is this account", nothing
                    # else. The column is at a fixed offset, not searched for.
                    if text and not is_sel and w > USAGE_COL + 1:
                        stdscr.addnstr(y, USAGE_COL, text[: w - 1 - USAGE_COL],
                                       w - 1 - USAGE_COL, pairs.get(sev, 0))
                    if details:
                        for line_text, line_sev in usage_detail(row):
                            y += 1
                            if y >= h - 1:
                                break
                            stdscr.addnstr(y, USAGE_COL, line_text,
                                           w - 1 - USAGE_COL,
                                           pairs.get(line_sev, 0))
                        y += 1  # a blank line, or the accounts run together
                y += 1
            if message and h > 1:
                stdscr.addnstr(h - 1, 0, message[: w - 1], w - 1, curses.A_BOLD)
            stdscr.refresh()

            try:
                ch = stdscr.getch()
            except KeyboardInterrupt:
                return
            if ch == -1:
                continue
            if ch in (ord("q"), 27):
                return

            row = rows[pickable[sel]] if pickable else None
            # A row is only a profile when it isn't the "nothing saved" filler.
            selected = row if row and row[0] == "profile" else None
            heads = [r[1] for r in rows if r[0] == "head"]
            kind = row[1] if row else (heads[0] if heads else None)

            if ch in (ord("j"), curses.KEY_DOWN) and pickable:
                sel = min(len(pickable) - 1, sel + 1)
            elif ch in (ord("k"), curses.KEY_UP) and pickable:
                sel = max(0, sel - 1)
            elif ch in (curses.KEY_ENTER, 10, 13) and selected:
                try:
                    message = switch(selected[1], selected[2]["slug"])
                except SwitchError as exc:
                    message = str(exc)
            elif ch == ord("s") and kind:
                ident = None
                live = BACKENDS[kind].read_live()
                if live is not None:
                    ident = BACKENDS[kind].identity(live)
                suggested = default_label(kind, ident) if ident else ""
                name = _ask(
                    stdscr, "Save profile", f"Name this {kind} login?", suggested
                )
                if name:
                    try:
                        message = save_live(kind, name)
                    except SwitchError as exc:
                        message = str(exc)
            elif ch == ord("r") and selected:
                name = _ask(
                    stdscr, "Rename profile",
                    f"New name for {selected[2]['label']}?", selected[2]["label"],
                )
                if name:
                    profile = selected[2]
                    delete_profile(profile["kind"], profile["slug"])
                    profile["label"] = name
                    profile["slug"] = _unique_slug(profile["kind"], name)
                    profile.pop("_active", None)
                    write_profile(profile)
                    message = f"renamed to {name}"
            elif ch == ord("d"):
                details = not details
            elif ch == ord("u"):
                message = "reading usage…"
                stdscr.addnstr(h - 1, 0, message.ljust(w - 1), w - 1, curses.A_BOLD)
                stdscr.refresh()
                usage = {"%s:%s" % (r["kind"], r["slug"]): r for r in usage_rows()}
                stale = [r["label"] for r in usage.values() if r["state"] != "live"]
                message = ("usage read" if not stale
                           else "usage read; cached for " + ", ".join(stale))
            elif ch == ord("x") and selected:
                profile = selected[2]
                # Deleting a profile removes this plugin's copy, never the
                # credential store the CLI reads — you stay logged in either way.
                note = None
                if profile.get("_active"):
                    note = "In use. You stay logged in; only the copy goes."
                if _confirm(
                    stdscr, "Delete profile",
                    f"Delete {profile['label']}?", note,
                ):
                    delete_profile(profile["kind"], profile["slug"])
                    message = f"deleted {profile['label']}"
                    sel = max(0, sel - 1)

    curses.wrapper(run)
    return 0


# ---- sidebar wiring -------------------------------------------------------

SIDEBAR_TABLE = "[ui.sidebar.agents]"
OVERRIDE_TABLE = "[ui.sidebar.agents.rows_by_agent]"
DEFAULT_ROWS = '[["state_icon", "workspace", "tab"], ["agent"]]'
CONFIG_BACKUP_DIR = os.path.join(STATE_DIR, "config-backups")
NAG_MARKER = os.path.join(STATE_DIR, ".sidebar-nagged")
_HEADER_RE = re.compile(r"\[\[?[^\[\]]+\]\]?\s*(#.*)?$")


def config_path():
    sock = os.environ.get("HERDR_SOCKET_PATH")
    if sock:
        guess = os.path.join(os.path.dirname(sock), "config.toml")
        if os.path.exists(guess):
            return guess
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


def _uncommented(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def _token_wired(text):
    return '"${}"'.format(TOKEN) in _uncommented(text)


def _match_brackets(text, i):
    while i < len(text) and text[i] != "[":
        if not text[i].isspace():
            return None
        i += 1
    depth = 0
    while i < len(text):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _find_value(text, header, key):
    """Offsets of the `key = [...]` array inside `header`, or None.

    Bracket depth is tracked so a row line such as `["agent"],` inside a
    multi-line value is never mistaken for the next table header.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if line.strip() == header:
            start = i
            break
    if start is None:
        return None
    offset = sum(len(l) for l in lines[: start + 1])
    depth = 0
    for line in lines[start + 1 :]:
        if depth == 0:
            if _HEADER_RE.match(line.strip()):
                return None
            match = re.match(r"\s*%s\s*=\s*" % re.escape(key), line)
            if match:
                vstart = offset + match.end()
                vend = _match_brackets(text, vstart)
                return None if vend is None else (vstart, vend)
        depth += line.count("[") - line.count("]")
        offset += len(line)
    return None


def _append_token(value):
    depth = 0
    inner_open = None
    for i, ch in enumerate(value):
        if ch == "[":
            depth += 1
            if depth == 2:
                inner_open = i
        elif ch == "]":
            if depth == 2 and inner_open is not None:
                inner = value[inner_open + 1 : i].strip()
                sep = ", " if inner else ""
                return value[:i].rstrip() + '%s"$%s"' % (sep, TOKEN) + value[i:]
            depth -= 1
    return None


def _drop_token(value):
    """Remove every "$TOKEN" entry from a rows array, or None if absent."""
    quoted = '"$%s"' % TOKEN
    if quoted not in value:
        return None
    out = re.sub(r",\s*" + re.escape(quoted), "", value)
    out = re.sub(re.escape(quoted) + r"\s*,\s*", "", out)
    out = out.replace(quoted, "")
    return out


def _valid_toml(text):
    """False only when tomllib is present and rejects the text."""
    try:
        import tomllib
    except ImportError:
        return True  # too old to check; the caller still keeps a backup
    try:
        tomllib.loads(text)
        return True
    except Exception:
        return False


def _stripped_config(text, kinds):
    """(new text, [what changed]) on success, else (None, reason)."""
    if not _token_wired(text):
        return None, "already"
    out, changed = text, []
    for header, key in (
        [(SIDEBAR_TABLE, "rows")] + [(OVERRIDE_TABLE, k) for k in kinds]
    ):
        span = _find_value(out, header, key)
        if not span:
            continue
        dropped = _drop_token(out[span[0] : span[1]])
        if dropped is None:
            continue
        out = out[: span[0]] + dropped + out[span[1] :]
        changed.append("rows" if key == "rows" else "rows_by_agent.%s" % key)
    if not changed:
        return None, "unparsed"
    return out, changed


def _merged_config(text, kinds):
    """(new text, [what changed]) on success, else (None, reason).

    An override in rows_by_agent replaces rows rather than extending it, so a
    kind listed there needs the token merged into its own layout too.
    """
    if _token_wired(text):
        return None, "already"
    out, changed = text, []
    span = _find_value(out, SIDEBAR_TABLE, "rows")
    if span:
        merged = _append_token(out[span[0] : span[1]])
        if merged is None:
            return None, "unparsed"
        out = out[: span[0]] + merged + out[span[1] :]
        changed.append("rows")
    elif SIDEBAR_TABLE in _uncommented(out):
        return None, "unparsed"
    else:
        block = "%s\nrows = %s\n" % (SIDEBAR_TABLE, _append_token(DEFAULT_ROWS))
        out = out.rstrip("\n") + "\n\n" + block
        changed.append("rows (new table)")
    for kind in kinds:
        span = _find_value(out, OVERRIDE_TABLE, kind)
        if not span:
            continue
        merged = _append_token(out[span[0] : span[1]])
        if merged is None:
            continue
        out = out[: span[0]] + merged + out[span[1] :]
        changed.append("rows_by_agent.%s" % kind)
    return out, changed


def sidebar_snippet():
    return "%s\nrows = %s" % (SIDEBAR_TABLE, _append_token(DEFAULT_ROWS))


def _rewrite_config(edit, kinds, done_word):
    """Apply `edit` to config.toml, backing the original up first.

    Returns (status, where, what changed). The edited text is parsed before it
    replaces anything, so a bad edit is refused rather than written.
    """
    path = config_path()
    if not os.path.exists(path):
        return "missing", path, []
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    out, info = edit(text, kinds)
    if out is None:
        return info, path, []
    if not _valid_toml(out):
        return "unparsed", path, []
    os.makedirs(CONFIG_BACKUP_DIR, exist_ok=True)
    backup = os.path.join(CONFIG_BACKUP_DIR, "config.toml.%d" % int(time.time()))
    with open(backup, "w", encoding="utf-8") as handle:
        handle.write(text)
    tmp = "%s.account-switch-tmp" % path
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(out)
    os.replace(tmp, path)
    herdr("server", "reload-config")
    return done_word, backup, info


def wire_sidebar(kinds):
    """Merge $TOKEN into the sidebar rows, keeping a backup of the original."""
    return _rewrite_config(_merged_config, kinds, "wired")


def unwire_sidebar(kinds):
    """Remove $TOKEN from the sidebar rows, keeping a backup of the original."""
    return _rewrite_config(_stripped_config, kinds, "unwired")


def warn_if_unwired():
    """A token nothing references renders nothing, and herdr reports no error.

    Startup says so once rather than editing config behind your back.
    """
    try:
        if not BADGE or os.path.exists(NAG_MARKER):
            return
        path = config_path()
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as handle:
            if _token_wired(handle.read()):
                return
        herdr(
            "notification", "show", "Accounts: badge not visible",
            "--body",
            "No sidebar row names $%s. Run the enable-badge action to add it."
            % TOKEN,
            "--sound", "none",
        )
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(NAG_MARKER, "w", encoding="utf-8") as handle:
            handle.write("")
    except Exception:
        pass


def cmd_enable_badge(argv):
    status, where, info = wire_sidebar(KIND_ORDER)
    if status == "wired":
        print("wired $%s into %s (%s)" % (TOKEN, config_path(), ", ".join(info)))
        print("backup: %s" % where)
        print("config reloaded")
    elif status == "already":
        print("$%s is already named in the sidebar rows — nothing to do." % TOKEN)
    elif status == "missing":
        print("no herdr config at %s\n\nadd:\n\n%s" % (where, sidebar_snippet()))
        return 1
    else:
        print(
            "could not edit %s safely — add $%s by hand:\n\n%s"
            % (where, TOKEN, sidebar_snippet())
        )
        return 1
    return 0


def cmd_disable_badge(argv):
    """Stop showing the pane badge: leave the rows, then clear what is stamped.

    Removing the token from the rows is what hides it. Clearing the panes as
    well means nothing is left behind for a later `$acct` row to resurrect.
    """
    status, where, info = unwire_sidebar(KIND_ORDER)
    agents = live_agents() or {}
    for pane_id in agents:
        _clear_badge(pane_id)
    if status == "unwired":
        print("removed $%s from %s (%s)" % (TOKEN, config_path(), ", ".join(info)))
        print("backup: %s" % where)
        print("cleared the badge from %d pane(s); config reloaded" % len(agents))
    elif status == "already":
        print("$%s is not named in the sidebar rows — nothing to remove." % TOKEN)
        print("cleared the badge from %d pane(s)" % len(agents))
    elif status == "missing":
        print("no herdr config at %s" % where)
        return 1
    else:
        print(
            "could not edit %s safely — remove \"$%s\" from the rows by hand"
            % (where, TOKEN)
        )
        return 1
    return 0


DISPATCH = {
    "ui": cmd_ui,
    "enable-badge": cmd_enable_badge,
    "disable-badge": cmd_disable_badge,
    "open": cmd_open,
    "next": cmd_next,
    "switch": cmd_switch,
    "save": cmd_save,
    "status": cmd_status,
    "stamp": cmd_stamp,
    "badge": cmd_badge,
    "list": cmd_list,
    "usage": cmd_usage,
    "usage-ui": cmd_usage_ui,
    "open-usage": cmd_open_usage,
}


def main():
    # Any invocation repoints the stable symlink, not just the startup hook.
    # An update moves this directory, and herdr does not re-run startup hooks
    # on install, so the link would otherwise stay stale until the next herdr
    # start — long enough for the badge reading through it to disappear.
    refresh_stable_link()
    if len(sys.argv) < 2 or sys.argv[1] not in DISPATCH:
        print(f"usage: switcher.py {{{'|'.join(DISPATCH)}}} [args]", file=sys.stderr)
        return 2
    try:
        return DISPATCH[sys.argv[1]](sys.argv[2:]) or 0
    except SwitchError as exc:
        print(f"account-switch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
