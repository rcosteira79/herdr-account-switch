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
herdr plugin install rcosteira79/herdr-plugins/account-switch
```

Or link a local checkout: `herdr plugin link /path/to/herdr-plugins/account-switch`.
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
  { type = "command", command = "python3 /path/to/account-switch/switcher.py badge claude", interval_seconds = 30, timeout_seconds = 5 },
  { type = "text", text = "  " },
]
tab_bar_right_separator = ""
```

The last item on the tab bar sits flush against the right edge, which reads as
cramped. A whitespace-only `text` entry after the badge is the inset — herdr
keeps it. Blank `tab_bar_right_separator` when you add one, or the default ` · `
lands between the badge and the inset. Drop both lines if you want the badge
hard against the edge.

`herdr plugin list --plugin rcosteira.account-switch --json` reports the path.
The `badge` command prints one line and exits — herdr re-runs it on the
interval, so this needs no daemon and no `[[startup]]` hook. It reads the
credential store and nothing else: no socket, no writes. Naming a kind
(`badge claude`) prints that kind alone; `badge` with no argument prints every
kind it can name, each prefixed with the kind.

Set `ACCOUNT_SWITCH_BADGE=0` in herdr's environment to stop stamping panes once
the tab bar shows it, or keep both — they read the same value.

#### In ccstatusline instead

[ccstatusline](https://github.com/sirmalloc/ccstatusline) drives the Claude Code
status line, and its **Custom Command** widget runs a shell command and shows
the output — the same one-line `badge` command, no herdr involved:

```json
{
  "type": "custom-command",
  "commandPath": "python3 /path/to/account-switch/switcher.py badge claude",
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

For a `tab_bar_right` entry, set them inline on the command — that env is yours
to write, unlike the one herdr hands to plugin actions:

```toml
command = "ACCOUNT_SWITCH_GLYPH=🔑 python3 /path/to/switcher.py badge claude"
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
command = "python3 /path/to/account-switch/switcher.py switch claude work"
```

## Picker keys

```
j / k or ↓ / ↑   move selection
enter            switch to the selected account
s                save the current login as a new profile (prompts for a name)
r                rename the selected profile
x                delete the selected profile, including the one in use
q / esc          close
```

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
2. **auto-saves the outgoing login** if it isn't in any profile yet — you can
   never overwrite an account the plugin doesn't already have a copy of;
3. writes a timestamped backup of it under `HERDR_PLUGIN_STATE_DIR/backups/`
   (last 10 kept);
4. reads the store back and compares the account identity, and **puts the
   previous login back** if the write didn't take.

An account already logged in but never saved shows up in the picker as
*unsaved*; press `s` before switching away if you want to name it yourself
rather than take the `autosaved-…` name.

## Config (env)

| var | default | meaning |
|-----|---------|---------|
| `ACCOUNT_SWITCH_NOTIFY` | `1` | herdr notification on each switch |
| `ACCOUNT_SWITCH_BADGE` | `1` | write the `$acct` pane token |
| `ACCOUNT_SWITCH_BADGE_ALWAYS` | `0` | badge a kind with no saved profile too |
| `ACCOUNT_SWITCH_GLYPH` | `👤` | the badge character |
| `ACCOUNT_SWITCH_BADGE_FORMAT` | `{glyph} {name}` | badge layout; either field may be dropped |
| `ACCOUNT_SWITCH_SEPARATOR` | ` · ` | between kinds, when `badge` prints more than one |
| `HERDR_BIN_PATH` | `herdr` | herdr binary (set by herdr when it invokes an action) |

Actions inherit the herdr server's environment, so setting these for the pane
badge means exporting them before starting herdr. The `tab_bar_right` and
ccstatusline entries take them inline on the command instead, which is why those
two are the easy places to restyle the badge.

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
