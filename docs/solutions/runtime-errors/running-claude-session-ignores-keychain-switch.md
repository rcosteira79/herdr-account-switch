---
module: ":account-switch"
date: 2026-10-28
problem_type: runtime_error
component: datastore
severity: high
symptoms:
  - "A claude switch completes and the badge moves, but running sessions keep hitting the old account's usage limit"
  - "A new claude process gets the new account while a running one beside it does not"
  - "Asking again after the limit message returns the same limit, with the old account's reset time"
root_cause: logic_error
resolution_type: code_fix
tags: [claude-code, keychain, credentials-file, mtime, running-session, switch]
---
# Problem

On macOS a switch writes the Keychain item. A running Claude Code session holds
its access token in memory, and drops it for the store's only when it decides
the login changed from outside. It decides that by the modification time of
`.credentials.json` in its config directory, not by the Keychain. With no such
file it re-reads the store. With the file present and its mtime unchanged, it
keeps the token it holds, and a 429 does not change that.

On this machine the file existed, holding only `mcpOAuth` entries and untouched
since 2026-10-06, so no switch reached a running session. The README had said
both CLIs re-read their store on every request. For Claude Code that does not
hold.

# Evidence

Established 2026-10-28 on Claude Code 2.1.311 (the stuck pane) and 2.1.312 (the
reproduction), macOS.

- The stuck pane carried no `CLAUDE_CODE_OAUTH_TOKEN` and had never run `/login`.
  Its limit errors came 70 to 87 seconds after the switch, carried request IDs,
  and named the old account's reset time. That account's session window was full.
- An isolated reproduction shared nothing with the panes. Setting
  `CLAUDE_CONFIG_DIR` to a temporary directory makes Claude Code use its own
  Keychain item, `Claude Code-credentials-` followed by the first 8 hex digits of
  the sha256 of that path. Only access tokens were copied into it, each with a
  dummy refresh token, so no real refresh chain could be spent. A
  `claude -p --input-format stream-json` session asked once on the limited
  account, the item was swapped to the other account, and the session asked again
  40 seconds later, past the 30-second Keychain read cache.

  | after the swap | second answer |
  |---|---|
  | file present, mtime unchanged (run twice) | the old account's limit |
  | no file | the new account |
  | file present, mtime moved | the new account |
  | swapped through `ClaudeBackend.write_live` | the new account |

- In the 2.1.311 bundle the check stats `<config dir>/.credentials.json` and
  compares `mtimeMs` with the last value it saw. A missing file falls through to
  re-reading the store. The Keychain read is cached for 30 seconds. The names are
  minified and change between builds: search the binary for
  `lastCredentialsMtimeMs` to find the check.

# What Didn't Work

- Assuming a running session re-reads the store on every request.
- Waiting it out. A 429 is not a 401, so it never triggers a re-read: the session
  stays on the old account until its own access token expires or is refused.
- Deleting the file. A missing file does make every session re-read, but this one
  holds MCP logins, and they are not the plugin's to delete.

# Solution

`ClaudeBackend.write_live` moves that file's mtime after a Keychain write when the
file exists. It leaves the contents alone and never creates the file.

Running sessions stuck on an old account recover without a switch:
`touch ~/.claude/.credentials.json`.

On Linux the store is that file, so writing it should move the mtime by itself.
That was not tested.

# Prevention

- A test that a switch "took" proves nothing about running sessions. Only a
  session that was already running when the store changed can show it, which is
  what the isolated reproduction above does.
- After a Claude Code update, if running sessions stop following switches again,
  search the new binary for `lastCredentialsMtimeMs` to see whether the check
  moved or now keys off something else.

# See also

`docs/solutions/runtime-errors/oauth-token-in-the-environment-pins-panes.md` has
the same symptom from a different cause: a token in the environment outranking
the store.
