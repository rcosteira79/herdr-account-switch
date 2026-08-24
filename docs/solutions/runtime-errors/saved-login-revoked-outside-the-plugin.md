---
module: ":account-switch"
date: 2026-08-24
problem_type: runtime_error
component: datastore
severity: high
symptoms:
  - "A saved profile installs cleanly, the switch reports success, and the agent is signed out"
  - "A parked profile's access token is still valid by its own exp claim, but the provider answers 401"
  - "codex: a profile stops working with no switch, no renewal and no action by this plugin"
root_cause: logic_error
resolution_type: code_fix
tags: [oauth, refresh-token, revocation, codex, claude-code, credential-store]
---
# Problem

A credential snapshot can stop working while sitting on disk, and nothing about
the snapshot shows it. It still parses, still installs, and only fails when an
agent uses it. `switch()` used to read the store back and compare the account
identity, which proves the write landed and says nothing about whether the login
still signs in.

Three provider rules cause it:

**A refresh token is single-use and rotates.** Renewing spends the stored one and
the reply carries its replacement. Reusing a spent token is permanent — the
provider answers `refresh_token_reused` and that chain is finished, which costs a
browser login to repair.

**A refusal is definitive only when the provider says so in words:**

| status | code | meaning |
|---|---|---|
| 401 | `token_revoked` | the grant was revoked, whatever the token's `exp` claims |
| 401 | `token_invalidated` | the same, worded differently by ChatGPT |
| 400 | `invalid_grant` | the refresh token is not accepted |
| 400 | `refresh_token_reused` | it was already spent |

A JWT's `exp` is a claim, not proof. A token the clock still calls valid gets a
401 once its session is superseded.

**Codex revocation is scoped to the installation, not to the account.**
`CODEX_HOME` contains an `installation_id`, and signing in revokes only grants
issued under *that* id — including grants for a different account. So a
`codex login`, from the CLI or from Codex desktop which share `~/.codex`, kills
whatever that home held before. A grant obtained under a separate `CODEX_HOME`
survives it.

# What Didn't Work

- Trusting the token's `exp`. Both providers hand out tokens that outlive their
  session, so the clock cannot tell a live login from a dead one.
- Reading the credential store back after writing it. That catches a store that
  refused the write. It cannot catch a credential the provider has retired.
- Treating any 401 or 400 as proof. A timeout, a 429 or no network prove nothing
  either way, and refusing a switch on those would block a working account.

# Solution

Ask the provider before overwriting a working login, while it is still in place.
`proven_payload` renews the target profile, reads the usage endpoint, and on a
401 renews past the clock and asks once more. It refuses the switch only on a
definitive answer from the table above. `verify_switch = false` turns it off.

Renewing rotates the tokens, so the check hands back the payload to install and
`switch()` writes *that* one to the profile. Writing the old one back would park
a spent pair over a live one — the failure this exists to prevent.

# Why This Works

The provider is the only authority on whether a login works. Everything local —
the file parsing, the identity matching, the expiry claim — describes the
snapshot, not the session behind it. Asking costs one HTTP round trip per switch
and, when the target's access token is clock-expired, one refresh rotation.

# Prevention

- Never treat a parseable snapshot as a working login.
- Never treat a network failure as a dead account. Only the words in the table.
- Persist a renewal's reply before using it: from the moment the endpoint
  answers, the old refresh token is dead and the reply is the only usable pair
  that account has. `renew_profile` stages it to disk first for that reason.
- Refresh a profile's snapshot as you park it, so a switch away does not leave a
  copy that can no longer renew.
- For codex, expect a login anywhere in `~/.codex` — including Codex desktop — to
  revoke the previous grant in that home.

Covered by `test_switcher.py`: a retired profile is refused with the live store
untouched and nothing written, a spent refresh token is refused, and being
offline or rate limited both switch through.
