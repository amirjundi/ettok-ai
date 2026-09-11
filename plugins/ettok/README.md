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

# The plugin is opt-in, so the runtime's own test suite stays unaffected.
hermes plugins enable ettok
```

## Pair with a platform

The agent is open source, so anyone can run it. Being able to reach a platform and
being allowed to use it are different things, and pairing is how the second is
granted.

```bash
ettok connect --platform https://your-platform-host
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
that have no activation gate and terms with no exemptions.

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
