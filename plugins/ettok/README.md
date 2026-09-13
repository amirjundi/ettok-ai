# Ettok AI — installing and running the agent

Hate speech monitoring for minority communities in Iraq. This is the agent half;
it talks to an Ettok platform, which owns the lexicon, the tropes, the cases and
the review queue.

## What the two halves do

The **platform** owns the knowledge and the verdict. It holds the terms, the
tropes, the exemptions, the rubric and the cases; it re-evaluates everything the
agent submits, and its conclusion is the one that stands.

The **agent** owns collection and delivery. It fetches knowledge at the start of
every run and keeps none of it, captures evidence before an item counts as
collected, forms an advisory opinion when there is budget for one, and gets
findings onto the platform without losing or duplicating any.

That split is why a curator's edit reaches every agent on its next run with no
redeployment, and why a stolen or reimaged laptop leaks nothing that is not
already on the server.

## Install

Python 3.11 is required and `uv` will fetch its own — the runtime caps below 3.14
deliberately, because its Rust-backed dependencies have no wheels above it.

```bash
git clone https://github.com/amirjundi/ettok-ai.git
cd ettok-ai

uv venv --python 3.11
uv pip install -e ".[all,dev]"
```

**Then activate the environment.** The `ettok` command is installed inside the
virtual environment, not system-wide, so without this step the shell reports
`ettok: not recognized` even though everything installed correctly.

Windows, PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Windows, cmd.exe:

```bat
.venv\Scripts\activate.bat
```

macOS and Linux:

```bash
source .venv/bin/activate
```

If PowerShell refuses with an execution-policy error, use `cmd.exe` instead, or
skip activation altogether and prefix commands with `uv run` — `uv run ettok
doctor` works from a fresh shell with nothing activated, which is also the form
to use in a scheduled task or a service.

```bash
# The plugin is opt-in, so the runtime's own test suite stays unaffected.
ettok plugins enable ettok

# Then let it walk you through the rest.
ettok setup
```

`ettok setup` asks which platform to report to, requests access from an
administrator, checks whether a model is configured, reports what the agent can
actually detect with the knowledge currently curated, and offers to schedule
unattended runs. It is safe to run again — every step detects what is already
done.

Run from a script rather than a terminal, it skips anything that would wait on a
person and tells you what is left, instead of hanging on a prompt nobody can see.

## Before the agent browses

Collection needs a real browser. Install it now rather than letting the agent ask
mid-run — an unattended run has nobody to answer, and an attended one gets
interrupted by a question about npm at the worst moment.

```bash
npm install -g agent-browser
agent-browser install
```

`ettok doctor` reports whether it is present.

### A note on Windows terminals

Use **PowerShell**, **cmd.exe** or **Windows Terminal**. Git Bash and MinTTY do
not give Python a real console, so arrow-key menus and yes/no prompts render but
do not accept keystrokes — the prompt appears and nothing you press reaches it.

If a menu asks for a number instead of offering arrow keys, `curses` is missing;
re-run `uv pip install -e .` to pick up `windows-curses`.

## Pair with a platform

The agent is open source, so anyone can run it. Being able to reach a platform and
being allowed to use it are different things, and pairing is how the second is
granted.

```bash
ettok connect --platform https://your-platform-host

# or, from a shell with nothing activated:
uv run ettok connect --platform https://your-platform-host
```

It prints a short code and a link, then waits. An administrator opens the link
while signed in, sees which machine is asking and by what name, and approves. The
key is then handed to this machine directly and written to `.env`.

Nothing secret travels through a human channel. The code you read aloud is
single-use, expires in minutes, and cannot be used to collect the key — polling is
keyed on a token that never leaves this machine.

```bash
ettok doctor        # the first thing to run afterwards
```

`doctor` answers "is this actually working" without reading logs or a database.
It checks pairing, the local database, the evidence directory, the platform
connection, the knowledge fetch and the browser tooling, and it reports how many
tropes still have no activation gate — which is how much of the detection
capability is still waiting on curators rather than on code.

## Run it

```bash
ettok status                    # pairing, open cases, delivery queue
ettok schedule --every 6h       # run unattended; survives restarts
ettok outbox status             # what is waiting to be delivered
ettok outbox drain              # send it now
```

Scheduling goes through the runtime's cron, not a timer inside the agent, because
cron survives a reboot and a closed laptop.

## Configuration

| Setting | Where | Notes |
|---|---|---|
| `ETTOK_PLATFORM_URL` | `.env` or `--platform` | Defaults to `http://localhost:8000` |
| `ETTOK_AGENT_ID` / `ETTOK_AGENT_KEY` | `.env` | Written by `ettok connect`. Do not set by hand |
| Classifier model | `auxiliary.ettok_classify` in `config.yaml` | Routes classification separately from the chat model |
| Pacing | `min_delay_seconds`, `max_delay_seconds` | Human pacing is a survival setting, not politeness |

## What it will and will not do

It never closes a case, never relaxes a deadline or a budget, and never
circumvents an access control. A CAPTCHA or a block is the platform saying it has
noticed; the agent stops and reports rather than working around it, because
solving the challenge removes the only warning while leaving the detection in
place.

Its verdict is advisory. The platform re-evaluates every submission and a human
reviews it before anything is reported.

## If it finds nothing

Check `ettok doctor` and the `readiness` block in a scan result before assuming
the feed is quiet. Two kinds of silence look identical from outside and are both
curator work rather than bugs:

- **A group with no topic markers** cannot be recognised in a feed at all, so its
  tropes never get a chance to fire however well they are curated.
- **A trope with no surface forms** has no literal text to match and is inert,
  however complete it looks in the admin.

The platform's `export_curator_workbook` reports both counts, along with tropes
that have no activation gate and terms with no exemptions. `ettok eval` reports
them too, and scores the curated lexicon against a fixed set of sentences from
the field data, so "is detection any good today" has an answer that does not
depend on anyone's memory.

A third kind of silence is not curator work at all:

- **Selectors that no longer match the page.** Social platforms change their
  markup without notice, and an extractor that finds nothing returns an empty
  page, which is indistinguishable from a page with no comments on it.

## Checking the selectors without a live session

Collection has never run against a live platform, so the Facebook selectors are
written against markup nobody has confirmed. Identity capture, the per-post
rates and the accounts page all rest on them.

Checking them needs no account and no login. Open a post in a browser, scroll
until the comments have loaded, expand "view more comments", save the page
(Ctrl+S, *Webpage, Complete* — or paste `document.documentElement.outerHTML`
into a file), then:

```bash
ettok selectors ~/Downloads/post.html
```

It runs the collector's own extraction JavaScript against that page and reports
what it would have got: the parent post, the comments, and how many of them
carry a name, a profile link and a permalink. The three are counted separately
because they fail separately — comments with no profile links means detection
still works while repeat-offender tracking is dead, and comments with no
permalinks means every finding cites the page rather than itself.

When something is wrong, try a candidate set against the same page before
committing to it:

```bash
ettok selectors ~/Downloads/post.html --try '{"post": "[role=main]", "comment": ".x1y1aw1k", "author": "a span"}'
```

and save the set that works with `ettok_save_selectors`, which is what a scan
will then use.

Two things it cannot tell you: whether the page you saved is representative, and
whether the markup changes next week. It answers one question — would today's
selectors have found the comments on this page — and that question is currently
unanswered.

## Development

```bash
.venv/Scripts/python -m pytest plugins/ettok/tests -q
```

Tests live here rather than in the runtime's own `tests/` tree, so nothing this
fork adds lands in a directory upstream also edits and `git merge upstream/main`
stays a merge.

One of them loads the platform's `normalize.py` directly and asserts
character-for-character agreement. The agent prefilters against a lexicon that
lives on the platform, which only works if both sides reduce text identically —
and a divergence raises nothing, it just quietly stops matching terms.
