# Working in this repo

`switcher.py` is the whole plugin: standard library only, no build step. Run the
checks with `python3 test_switcher.py` — they use a temporary state directory and
a fake credential backend, and never touch a real login.

Solved problems that cost real investigation live in `docs/solutions/`, stamped
with how each was established — relevant when changing anything about renewal,
switching, or the credential stores.
