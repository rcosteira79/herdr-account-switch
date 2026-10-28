---
module: ":account-switch"
date: 2026-10-28
problem_type: runtime_error
component: datastore
severity: high
symptoms:
  - "A claude switch completes and the badge moves, but panes keep hitting the old account's usage limit"
  - "Only panes opened after some date are affected, while older panes follow the switch"
  - "/login in a pane fixes that pane, and the next new pane is back on the old account"
  - "/login warns that CLAUDE_CODE_OAUTH_TOKEN was set in your environment"
root_cause: config_error
resolution_type: config_change
tags: [claude-code, oauth-token, environment, shell-profile, setup-token, badge]
---
# Problem

Claude Code prefers `CLAUDE_CODE_OAUTH_TOKEN` to the login in its credential
store. A switch rewrites only the store, so a session started with the variable
set never sees it. The badge reads the store, so it moves with the switch and
names an account that session is not billing.

The variable came from `~/.zshenv`, which exported a long-lived
`claude setup-token` token read out of a keychain item. zsh reads that file once,
at shell start. Every pane opened after the edit carried the token. Panes opened
before it did not — including those where `claude` was relaunched inside an old
shell. That split, new panes stuck and old panes fine, is the tell.

# Evidence

Established 2026-10-28 on Claude Code 2.1.312, macOS.

- `claude auth status --json` in a pane reported `"authMethod": "oauth_token"`.
  With `env -u CLAUDE_CODE_OAUTH_TOKEN` it reported `"claude.ai"` and the
  store's account. This is the quickest way to see which login a shell gets.
- The token matched none of the saved profiles' tokens. It is a separate grant,
  fixed to one account.
- The profile endpoint will not say whose token it is: `/api/oauth/profile`
  answers 403, because a setup-token carries no `user:profile` scope. The usage
  endpoint was rate limiting it. A one-token `/v1/messages` call does say, through
  its `anthropic-ratelimit-unified-7d-utilization` and `-7d-reset` headers. Those
  matched one profile's cached weekly window exactly, and the limit message's
  reset time matched that profile's cached session `resets_at`.
- `ps -E -ww -o pid=,command= -p <pids>` showed which running `claude` processes
  carried it: 3 of 22, all started after the edit. On macOS `pgrep` leaves out
  the caller's own ancestors by default, so it misses the session it is run
  from. `ps -A` does not.
- herdr's autocontinue log timestamps each pane's usage wall. Every wall after
  the switch was in a pane carrying the token.

Not established: whether a session that ran `/login` after starting with the
variable follows later switches. Its environment still carries the variable,
so the switch warning counts it.

# What Didn't Work

- `/login` in each pane. It recovers that session only, as its own message says,
  and the next pane opened from a shell with the export is pinned again.
- Trusting the badge. It reports the store, not what a session bills.
- Asking the profile endpoint which account the token belongs to.

# Solution

- The export was removed from `~/.zshenv`. Running sessions have to restart from
  a shell without it: relaunching `claude` in the same shell inherits it again,
  so `unset CLAUDE_CODE_OAUTH_TOKEN` first or open a new pane.
- A claude switch now counts the running `claude` processes whose starting
  environment sets the variable, and says so in its result and its notification.
  See `pinned_sessions` and `_process_environments` in `switcher.py`.

# Why This Works

Without the variable, Claude Code reads the store, and the store is what a switch
writes. The warning reads each process's environment as it was at exec, which is
the environment its login came from.

# Prevention

- When a switch takes but sessions do not follow, run `claude auth status --json`
  in the pane before suspecting the store or the switch.
- Do not export `CLAUDE_CODE_OAUTH_TOKEN` in a shell profile on a machine that
  runs agents under this plugin. A tool that needs a long-lived token can be given
  it on its own command line.

# See also

`docs/solutions/runtime-errors/running-claude-session-ignores-keychain-switch.md`
has the same symptom with no token in the environment: a running session that
never notices the Keychain changed.
