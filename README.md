# account-switch

Hot-swap Claude Code / Codex logins from inside [herdr](https://herdr.dev),
without logging out and back in. For the case: your main account hits its
window at 11am, you have a second one, and you don't want to re-auth in a
browser to use it — or to find out three panes later which account the fleet
has been billing.

## What it does

- **Named profiles** per agent kind. A profile is a snapshot of the credential
  store the CLI actually reads.
- **Switch** from an overlay picker, or cycle to the next account with one
  keybinding — no UI, no prompt.
- Both CLIs re-read their credential store on every request, so **agents that
  are already running switch too**, from their next turn. Nothing to restart.
- A **`$acct` badge** on every claude/codex pane naming the account in use, so
  the sidebar answers "which account is this burning" at a glance. It appears as
  soon as one profile is saved: `👤 work` for a saved login, `👤 work*` for a
  login no profile has a copy of, `👤 logged out` for none.

### No daemon

Unlike the other plugins here, this one is pure action — nothing runs in the
background. Credentials only change when you press a key, so there is nothing
to react to. The cost is that the `$acct` badge is refreshed at the moments the
plugin runs (a switch, a save, herdr startup, while the picker is open) rather
than continuously; a pane opened after the last switch shows no badge until the
next one. That is the whole trade, and it beats a daemon polling forever to
restate a value that changes twice a day.

## Install

```sh
herdr plugin install rcosteira79/herdr-account-switch
```

Or link a local checkout: `herdr plugin link /path/to/herdr-account-switch`.
Re-run `install`/`link` after a `herdr update` — updates drop plugins.

Then, with the account you use most logged in:

```sh
herdr plugin action invoke save --plugin rcosteira.account-switch
```

That snapshots whatever is logged in right now. Log into the other account the
normal way (`claude` → `/login`, or `codex login`), run `save` again, and you
have two profiles to switch between.

### Config (`~/.config/herdr/config.toml`)

**1. Show the badge.** `$acct` is a pane token, and a token renders only if a
sidebar row names it. Installing a plugin does not edit your config, so until
some row names `$acct`, the badge is invisible and herdr reports no error. The
`enable-badge` action does that one edit for you:

```sh
herdr plugin action invoke enable-badge --plugin rcosteira.account-switch
```

It backs `config.toml` up first, merges `$acct` into the rows you already have
(and into any `rows_by_agent` override for claude/codex, since an override
replaces `rows` rather than extending it), then reloads the server. Run it
twice and the second run does nothing. To do it by hand instead:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab", "$acct"], ["agent"]]
```

The `stamp` action checks this at startup and posts one notification if the
token is unreferenced. It never edits the config on its own — that only happens
when you invoke `enable-badge`. It touches the sidebar row only — it does not
add, remove or switch accounts. That is the picker, under `open`.

#### Where to show it

The account is the same on every pane of a kind, so there are three places to
put it and they are not exclusive. All three read the same value.

| where | how | scope |
|-------|-----|-------|
| sidebar, per pane | `$acct` token, via `enable-badge` | one badge per agent row |
| herdr tab bar | `tab_bar_right` command entry | one badge for the whole session |
| Claude Code status line | ccstatusline `custom-command` widget | one badge per Claude pane |

#### One badge instead of one per pane

Credentials are machine-wide per kind, so every claude pane shows the same
account and the sidebar repeats itself down the column. To state it once, put it
in the tab bar instead and turn the pane token off:

```toml
[ui]
tab_bar_right = [
  { type = "command", command = "python3 ~/.local/state/herdr/plugins/rcosteira.account-switch/current/switcher.py badge claude", interval_seconds = 30, timeout_seconds = 5 },
  { type = "text", text = "  " },
]
tab_bar_right_separator = ""
```

The last item on the tab bar sits flush against the right edge, which reads as
cramped. A whitespace-only `text` entry after the badge is the inset — herdr
keeps it. Blank `tab_bar_right_separator` when you add one, or the default ` · `
lands between the badge and the inset. Drop both lines if you want the badge
hard against the edge.

That path is a symlink the plugin repoints at itself on every herdr start, so it
keeps working after an update. Naming the install directory directly does not:
it carries a content hash, so an update moves it, and herdr clears a command's
value when it fails — the badge would vanish with nothing to explain it.
`herdr plugin list --plugin rcosteira.account-switch --json` reports the real
directory if you need it.
The `badge` command prints one line and exits — herdr re-runs it on the
interval, so this needs no daemon and no `[[startup]]` hook. It reads the
credential store and nothing else: no socket, no writes. Naming a kind
(`badge claude`) prints that kind alone; `badge` with no argument prints every
kind it can name, each prefixed with the agent's name:

```
Claude 👤 work · Codex 👤 spare
```

The badge cannot vary per pane or per tab, and that is not a limitation of the
badge. A credential store is machine-wide per agent, so every claude pane bills
to the same account; a per-pane badge would print the same value on each one.
Printing both accounts once, on the tab bar, says everything there is to say.

Set `ACCOUNT_SWITCH_BADGE=0` in herdr's environment to stop stamping panes once
the tab bar shows it, or keep both — they read the same value.

#### In ccstatusline instead

[ccstatusline](https://github.com/sirmalloc/ccstatusline) drives the Claude Code
status line, and its **Custom Command** widget runs a shell command and shows
the output — the same one-line `badge` command, no herdr involved:

```json
{
  "type": "custom-command",
  "commandPath": "python3 ~/.local/state/herdr/plugins/rcosteira.account-switch/current/switcher.py badge claude",
  "timeout": 2000
}
```

Add it through `ccstatusline`'s editor (Custom → Custom Command) rather than by
hand, so the widget gets an `id`. The widget also takes `maxWidth` to truncate
and `preserveColors` to keep ANSI colour from the command.

This works because `badge` needs nothing from herdr: it reads the credential
store, prints one line and exits. It runs the same from a herdr action, a bare
shell, or a Claude Code status line. Only the pane and workspace badges talk to
the herdr socket.

The trade is scope. The status line lives inside one Claude pane, so it names
the account for that pane's CLI — which is the same account everywhere, stated
once per pane rather than once per session. Pick the tab bar if you want it
stated exactly once.

#### Choosing the badge

`ACCOUNT_SWITCH_GLYPH` picks the character, and `ACCOUNT_SWITCH_BADGE_FORMAT`
decides what goes around it. Both fields are optional, so the format doubles as
a way to show less:

| format | shows |
|--------|-------|
| `{glyph} {name}` (default) | `👤 work` |
| `{glyph}{name}` | `👤work` — no space |
| `{glyph}` | `👤` — badge only, no account name |
| `{name}` | `work` — no glyph |
| `{agent} - {name}` | `Claude - work` — the agent's short name |
| `{title}: {name}` | `Claude Code: work` — its full name |

`{agent}` and `{title}` earn their place when the badge names two accounts at
once. A format that uses either one already says which agent it is, so the
automatic prefix steps aside. With `separator` set to three spaces:

```toml
badge_format = "{agent} - {name}"
separator = "   "
```

```
Claude - work   Codex - spare
```

A format naming a field that does not exist falls back to the default rather
than blanking the badge.

For a `tab_bar_right` entry, set them inline on the command — that env is yours
to write, unlike the one herdr hands to plugin actions:

```toml
command = "ACCOUNT_SWITCH_GLYPH=🔑 python3 ~/.local/state/herdr/plugins/rcosteira.account-switch/current/switcher.py badge claude"
```

**2. Keybindings** — `herdr server reload-config` after editing these:

```toml
[[keys.command]]
key = "prefix+a"              # cycle the focused agent's kind to its next account
type = "plugin_action"
command = "rcosteira.account-switch.next"
description = "next account"

[[keys.command]]
key = "prefix+shift+a"        # open the picker
type = "plugin_action"
command = "rcosteira.account-switch.open"
description = "account picker"
```

To jump straight to one named account, call the script instead — actions take
no arguments. `herdr plugin list --plugin rcosteira.account-switch --json`
reports where the plugin is installed:

```toml
[[keys.command]]
key = "prefix+1"
type = "shell"
command = "python3 ~/.local/state/herdr/plugins/rcosteira.account-switch/current/switcher.py switch claude work"
```

## Usage

What is left on every account you have saved, both agent kinds, in one panel:

```sh
herdr plugin action invoke usage --plugin rcosteira.account-switch
```

```
Claude Code
  ● main          Session    16%  ██░░░░░░░░  limit in 2h10 (resets in 10m)
                  Weekly     65%  ███████░░░  limit in 1d12h (resets in 4d02h)
                  Fable       6%  █░░░░░░░░░  ~88% left (resets in 4d02h)
    spare         Session     0%  ░░░░░░░░░░  —
                  Weekly      2%  ░░░░░░░░░░  ~92% left (resets in 5d07h)
Codex
  ● main          Weekly     42%  ████░░░░░░  ~34% left (resets in 4d01h)
```

`●` is the account that is live. The point of listing the others is the case
above: `main` runs out before its session renews, and `spare` has room now.

`r` re-reads, `q` closes. `switcher.py usage` prints the same thing as text
(`--color` for ANSI, `--json` for a machine to read); neither prints a
credential.

### Where the numbers come from

Each provider publishes the account's own allowance:

```
GET https://api.anthropic.com/api/oauth/usage        # claude
GET https://chatgpt.com/backend-api/wham/usage       # codex
```

Both are unofficial and can change or vanish. A read that fails leaves the row
saying so rather than showing a stale number as if it were current.

### Reading an account you are not on

Only the live account keeps a fresh token; a parked profile's has usually
expired. Reading one therefore renews it first, exactly as the CLI does on any
expiry, and as [openusage](https://github.com/robinebers/openusage) does to keep
its own panel current.

**Renewing spends the stored refresh token.** The reply carries a new pair and
the old one dies, so losing that reply would cost you a browser login. The order
guards against it: back up the profile, call the endpoint, write the reply to a
sidecar the instant it lands, rewrite the profile, clear the sidecar. A run that
dies mid-way is adopted by the next one. An access token lasts hours, so this is
roughly one renewal per account per token lifetime, not one per refresh.

`ACCOUNT_SWITCH_USAGE_RENEW=0` turns it off; parked accounts then show their
last-seen numbers with an age, and the live ones stay accurate.

### Colours

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_SWITCH_USAGE_THRESHOLDS` | `60,85` | percent at which a window turns warn, then crit |
| `ACCOUNT_SWITCH_USAGE_COLORS` | `ok=green,warn=yellow,crit=red,stale=blue` | the palette |
| `ACCOUNT_SWITCH_USAGE_BAR` | `█░` | filled and empty bar characters |
| `ACCOUNT_SWITCH_USAGE_BAR_WIDTH` | `10` | bar width in cells |

The overlay and the `--color` text form read the same palette, so they agree.

## Picker keys

```
j / k or ↓ / ↑   move selection
enter            switch to the selected account
s                save the current login as a new profile (prompts for a name)
r                rename the selected profile
x                delete the selected profile, including the one in use
d                show / hide every usage window, not just the binding one
u                re-read usage
q / esc          close
```

Each account carries how much of it is left, so the picker answers "which one
should I switch to" on its own:

```
       account               used resets in
 ● main             █████░░░  66% 3d23h  you@example.com · max
   spare            ░░░░░░░░   2% 5d04h  you@work.example · team
```

That column is the **binding** window — the one closest to stopping you,
preferring the one the API itself marks active. `d` swaps it for every window,
one account per block:

```
       account       window   used              at this rate
Claude Code
 ● main                                  you@example.com · max
                    Session    11%  █░░░░░░░░░  ~77% left (resets in 2h37)
                    Weekly     66%  ███████░░░  limit in 1d13h (resets in 3d23h)
                    Fable       7%  █░░░░░░░░░  ~84% left (resets in 3d23h)

   spare                                 you@work.example · team
                    Session     0%  ░░░░░░░░░░  —
                    Weekly      2%  ░░░░░░░░░░  ~92% left (resets in 5d04h)
                    Fable       0%  ░░░░░░░░░░  —

Codex
 ● main                                  you@example.com · plus
                    Weekly     66%  ███████░░░  limit in 1d13h (resets in 3d23h)
```

The account line drops its own figure while expanded, because the windows below
already carry it. Both views carry column titles.

**Fable** is a per-model weekly limit. The API calls it `weekly_scoped` and puts
the model in a `scope` field, so the model's name is shown instead of the key.

**Codex shows only its weekly window.** It reports one worth watching; the rest
is noise next to claude's three.

**at this rate** is the column that decides whether to switch: where the window
lands if you keep spending it as you have been. `~70% left` finishes with room
to spare; `limit in 1d13h (resets in 4d00h)` runs out three days early — 66%
used is comfortable with a day to go and a problem with five. The reset comes
along in brackets, because a projection means nothing without knowing what it is
racing.

It is worked out from the window's length and how much of it has already passed,
so it says nothing until the window has run long enough to mean something: a
window minutes old would otherwise claim the limit is imminent. A window already
at 100% reads `spent`.

Usage is read once when the picker opens and then comes from cache, because the
endpoints rate-limit. `u` forces a re-read.

`s`, `r` and `x` each open a dialog in the middle of the screen. Deleting removes
this plugin's saved copy of a login and nothing else: the credential store the
CLI reads is never touched, so you stay logged in. Delete the profile for the
account you are on and the next switch away re-saves it under an `autosaved-…`
name.

`●` marks the account that is live. A section header reading *"live: … (unsaved,
press s)"* means you are logged into something the plugin has no copy of yet.

## What it reads and writes

| kind | credential store | identity |
|------|------------------|----------|
| claude | Keychain item `Claude Code-credentials` (macOS), else `~/.claude/.credentials.json` | the `oauthAccount` block in `~/.claude.json` |
| codex | `~/.codex/auth.json` | `account_id` + the `id_token` claims inside it |

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` are honoured if you set them. On claude,
**only** the `oauthAccount` key of `~/.claude.json` is touched — projects,
history and onboarding state are rewritten verbatim.

Profiles live in `HERDR_PLUGIN_STATE_DIR/profiles/<kind>/<slug>.json`, mode
`0600` inside a `0700` directory. They are password-equivalent: the same
secrets that were already on the disk, in the same account, on the same
machine, and they never leave it.

## Not losing a login

Swapping a credential store is the one operation here that can actually cost
you something, so every switch:

1. takes an `flock`, so two switches (or a switch racing your own keybinding)
   can't interleave writes while a CLI is refreshing its token;
2. **asks the provider whether the saved login still works**, while the live
   one is still in place. Reading the store back proves the write landed. It
   says nothing about whether that account can still sign in, and the two come
   apart exactly when it matters: a login the provider has retired installs
   cleanly, reports success, and signs you out. Only a definitive refusal stops
   a switch — the provider answering 401 to a token it has just issued, or
   turning the refresh token down in words. Being offline, timing out and
   getting rate limited prove nothing either way, and none of them block a
   switch. Set `verify_switch = false` to skip the check;
3. **auto-saves the outgoing login** if it isn't in any profile yet — you can
   never overwrite an account the plugin doesn't already have a copy of — and
   **refreshes its snapshot** if it is. The CLI keeps renewing the live tokens,
   so a profile saved hours ago holds older ones; parking the account without
   updating it can leave a copy that is no longer able to renew, and that costs
   a browser login to repair;
4. writes a timestamped backup of it under `HERDR_PLUGIN_STATE_DIR/backups/`
   (last 10 kept);
5. reads the store back and compares the account identity, and **puts the
   previous login back** if the write didn't take.

An account already logged in but never saved shows up in the picker as
*unsaved*; press `s` before switching away if you want to name it yourself
rather than take the `autosaved-…` name.

## Config

Settings live in a file in the plugin's own config directory:

```sh
$(herdr plugin config-dir rcosteira.account-switch)/config.toml
```

```toml
# The badge character on each agent pane and in the tab bar.
glyph = "🔑"

# What goes around it, from {glyph}, {name}, {agent} and {title}. Drop {name}
# for a badge with no account name.
badge_format = "{glyph}{name}"

# Percent at which a usage window turns amber, then red.
usage_thresholds = "50,75"

# Override one colour of the four: ok, warn, crit, stale.
usage_colors = "warn=magenta"

# Renew a parked account's token so its usage can be read. Turning this off
# leaves parked accounts showing their last-seen numbers with an age.
usage_renew = false

# Ask the provider whether a saved login still works before installing it.
# Turning this off restores the old behaviour: a retired login installs
# cleanly and signs you out.
verify_switch = false
```

The key is the variable below without its `ACCOUNT_SWITCH_` prefix, lowercased.
`config.json` works too if you prefer it. A file that fails to parse is ignored
rather than fatal, and the defaults stand.

An environment variable still wins over the file, but is rarely the practical
choice: plugin actions inherit the **herdr server's** environment, so setting
one means exporting it before herdr starts. The `tab_bar_right` and
ccstatusline entries are the exception — those take variables inline on the
command, which is why they are the easy place to restyle the badge.

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_SWITCH_NOTIFY` | `1` | herdr notification on each switch |
| `ACCOUNT_SWITCH_BADGE` | `1` | write the `$acct` pane token |
| `ACCOUNT_SWITCH_BADGE_ALWAYS` | `0` | badge a kind with no saved profile too |
| `ACCOUNT_SWITCH_GLYPH` | `👤` | the badge character |
| `ACCOUNT_SWITCH_BADGE_FORMAT` | `{glyph} {name}` | badge layout, from `{glyph}`, `{name}`, `{agent}`, `{title}`; any may be dropped |
| `ACCOUNT_SWITCH_SEPARATOR` | ` · ` | between kinds, when `badge` prints more than one |
| `ACCOUNT_SWITCH_VERIFY_SWITCH` | `1` | ask the provider whether a saved login still works before installing it |
| `ACCOUNT_SWITCH_USAGE_RENEW` | `1` | renew a parked account's token so its usage can be read |
| `ACCOUNT_SWITCH_USAGE_TTL_S` | `120` | how long a usage answer is reused |
| `ACCOUNT_SWITCH_RENEW_MARGIN_S` | `300` | renew this far ahead of a token's expiry |
| `HERDR_BIN_PATH` | `herdr` | herdr binary (set by herdr when it invokes an action) |


## Fragility

Credential layouts are the agents' private business and can change without
notice. If a switch stops taking, `herdr plugin action invoke status --plugin
rcosteira.account-switch` prints what each store currently reports; the store
paths and the shape read out of them are the two constants at the top of
`ClaudeBackend` / `CodexBackend` in `switcher.py`.

A Codex install that keeps its credentials in the macOS Keychain rather than
`~/.codex/auth.json` is not supported — the plugin reports codex as not logged
in rather than guessing.

## Requirements

- herdr ≥ 0.8.0 (for the `[[startup]]` badge refresh)
- Python 3 (stdlib only; uses `curses` for the picker)
- macOS or Linux

## The other herdr plugins

Each installs on its own; they share nothing but an author.

- [**herdr-idle-shell-badge**](https://github.com/rcosteira79/herdr-idle-shell-badge) — Badges idle agents that still have a background shell running, so one that *looks* done but left a process alive isn't mistaken for finished.
- [**herdr-readpending**](https://github.com/rcosteira79/herdr-readpending) — Mark agents you started reading but haven't finished — a numbered badge plus a reorderable overlay queue that clears when you focus the agent.
- [**herdr-autocontinue**](https://github.com/rcosteira79/herdr-autocontinue) — Watch agents for usage-limit walls, badge the countdown to the reset, and re-prompt the agents you armed once the window reopens.
