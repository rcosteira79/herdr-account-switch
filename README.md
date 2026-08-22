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
  the sidebar answers "which account is this burning" at a glance.

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

Two edits. `herdr server reload-config` after any change.

**1. Show the badge** — `$acct` is a pane token, and tokens only render if a
sidebar row references them:

```toml
[ui.sidebar.agents]
rows = [["state_icon", "workspace", "tab", "$acct"], ["agent"]]
```

**2. Keybindings**:

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
x                delete the selected profile (never the one in use)
q / esc          close
```

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
| `ACCOUNT_SWITCH_BADGE_ALWAYS` | `0` | badge kinds with only one profile too |
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
