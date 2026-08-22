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

TOKEN = "acct"
UNSAVED_MARK = "*"  # live login that no profile has a copy of
KEEP_BACKUPS = 10
IS_MAC = platform.system() == "Darwin"


def _flag(name, default=True):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


NOTIFY = _flag("ACCOUNT_SWITCH_NOTIFY")
BADGE = _flag("ACCOUNT_SWITCH_BADGE")
# Badge a kind with no saved profile too.
BADGE_ALWAYS = _flag("ACCOUNT_SWITCH_BADGE_ALWAYS", default=False)
GLYPH = os.environ.get("ACCOUNT_SWITCH_GLYPH") or "\N{BUST IN SILHOUETTE}"  # 👤
# Both fields are optional: "{glyph}" is a badge with no text, "{name}" is text
# with no glyph.
BADGE_FORMAT = os.environ.get("ACCOUNT_SWITCH_BADGE_FORMAT") or "{glyph} {name}"
SEPARATOR = os.environ.get("ACCOUNT_SWITCH_SEPARATOR") or " · "


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
    would race the CLIs' own token refresh."""

    def __enter__(self):
        _secure_dir(STATE_DIR)
        self._f = open(LOCK, "w")
        fcntl.flock(self._f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self._f, fcntl.LOCK_UN)
        self._f.close()


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
        if live is not None:
            live_fp = _fingerprint(backend.identity(live))
            if live_fp and live_fp == target.get("fingerprint"):
                return f"{kind}: already on {target['label']}"
            # Never overwrite a login we don't have a copy of.
            if not any(p.get("fingerprint") == live_fp for p in list_profiles(kind)):
                ident = backend.identity(live)
                rescued = make_profile(kind, "autosaved-" + default_label(kind, ident), live)
                write_profile(rescued)
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


def cmd_stamp(argv):
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


def _profile_line(kind, p, width):
    backend = BACKENDS[kind]
    mark = "●" if p.get("_active") else " "  # ●
    desc = backend.describe(p.get("identity") or {})
    line = f" {mark} {p['label']:<16} {desc}"
    return line[: max(0, width - 1)]


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
        # Slow refresh on purpose: each tick re-reads the credential store, and
        # on macOS that is a Keychain lookup per agent kind.
        stdscr.timeout(5000)
        sel = 0
        message = ""
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
                stdscr.addnstr(
                    1, 0,
                    "j/k select · enter switch · s save current · r rename · x delete · q quit",
                    w - 1, curses.A_DIM,
                )
            y = 3
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
                    attr = curses.A_REVERSE if is_sel else curses.A_NORMAL
                    stdscr.addnstr(
                        y, 0, _profile_line(kind, body, w).ljust(w - 1), w - 1, attr
                    )
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
}


def main():
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
