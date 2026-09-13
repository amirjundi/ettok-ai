# Ettok AI

**A hate speech monitoring agent for minority communities in Iraq.**

Ettok AI reads public social media discourse for hate speech targeting Yazidi and
Iraqi Christian (Assyrian, Chaldean, Syriac) communities, and delivers
evidence-backed findings to the Ettok platform for expert review.

Hate speech here is episodic. A campaign flares against one community around an
incident, cools, goes dormant, and reactivates weeks later — sometimes against a
different community entirely. So the agent is organised around **cases**: bounded
monitoring episodes with defined targets, schedules, budgets and stop conditions,
all owned by the platform rather than decided by the agent.

## What makes it different from a hate-speech classifier

**Context.** The phrase *اعوذ بالله من الشيطان الرجيم* — "I seek refuge in God
from the accursed devil" — is said daily by millions. Under a road accident it is
ordinary piety. Under a post about Sinjar, aimed at Yazidis, it is the
devil-worship taunt, the oldest libel against that community and the one ISIS
used as justification.

Identical words. Only the post above them differs. So every comment is judged
together with what it replies to, against vocabulary curated by people from the
affected communities — not against a general-purpose model's idea of offence.

**The denominator.** Comments that match nothing are collected too. Nine findings
means little; nine out of four hundred is an ordinary thread and nine out of
twelve is a pile-on, and the two need different responses.

**Reporting without approval.** A finding is recorded the moment the agent sees
it. Nobody has to approve it for it to count, because an approval queue that
nobody empties is a silent off-switch. Review is correction: a person marks what
the agent got wrong, and their judgement replaces its own.

## Requirements

- Python 3.11 — [`uv`](https://docs.astral.sh/uv/) fetches its own, so you do not
  need it installed. The runtime caps below 3.14 deliberately: its Rust-backed
  dependencies have no wheels above that.
- Node.js 18+ for the dashboard and the browser tooling.
- A reachable Ettok platform to report to.

## Install

Identical on all three platforms apart from activation.

```bash
git clone https://github.com/amirjundi/ettok-ai.git
cd ettok-ai

uv venv --python 3.11
uv pip install -e ".[all,dev]"
```

Then activate the environment. Without this the shell reports
`ettok: not recognized` — the command lives inside the virtual environment, not
system-wide.

**Linux / macOS**

```bash
source .venv/bin/activate
```

**Windows — PowerShell**

```powershell
.venv\Scripts\Activate.ps1
```

**Windows — cmd.exe**

```bat
.venv\Scripts\activate.bat
```

If PowerShell refuses with an execution-policy error, use `cmd.exe`, or skip
activation entirely and prefix commands with `uv run` — `uv run ettok ettok
doctor` works from a fresh shell with nothing activated, and is the form to use
in a scheduled task or a service unit.

## First run

```bash
ettok plugins enable ettok      # the monitoring plugin is opt-in
ettok ettok setup               # platform, pairing, model, gateway, schedule
```

`ettok ettok` is not a typo. The runtime's binary is `ettok`, and the monitoring
agent registers its own commands underneath it, so its subcommands are reached as
`ettok ettok <command>`.

Setup walks through seven steps: which platform to report to, pairing this
machine, the model, what the agent can currently detect, trimming the tool list,
the dashboard chat backend, and whether to run unattended. It is safe to run
again.

Pairing needs an administrator on the platform to approve this specific machine.
Ettok AI is open source, so anyone can run the agent — approval is what lets a
particular machine see anything, and a lost laptop can be revoked without
touching the others.

## Running it

**The dashboard** — sessions, logs, configuration and a chat with the agent:

```bash
ettok dashboard                 # http://127.0.0.1:8123
```

**In the background, with no dashboard open.** This is what field machines want:
the agent keeps working after a reboot with nobody logged in.

```bash
ettok gateway install           # systemd, launchd, Windows Scheduled Task or s6
```

The runtime picks the right supervisor for the operating system. `ettok gateway
run` starts it in the foreground instead, which is useful when you want to watch
it and fine when you do not mind it stopping with the terminal.

**Scheduled scans**, which survive reboots and a closed laptop in a way an
in-process timer does not:

```bash
ettok ettok schedule --every 6h
ettok ettok schedule --remove --name ettok-scan
```

One job works every case in turn. Each case has its own interval and waits it
out, so an active campaign is scanned more often than a dormant watch and no case
starves.

## Checking it works

```bash
ettok ettok doctor              # pairing, database, browser, chat, knowledge
ettok ettok status              # open cases and the delivery queue
ettok ettok eval                # score the live lexicon against a fixed gold set
```

`doctor` answers "can this run". `eval` answers "would it be any good if it did",
which is a different question — it runs sixteen sentences from the field data
through the real matcher and reports what it got wrong, plus any curation debt.

**Before the first live collection**, check the extraction selectors against a
real page. Social platforms change their markup without notice, and an extractor
that finds nothing returns an empty page — indistinguishable from a page with no
comments on it.

```bash
ettok ettok selectors ~/Downloads/post.html
```

Save a post from the browser after scrolling until the comments load. It needs no
account and no login, and reports whether the comments, the profile links and the
permalinks would have been found.

## Updating

```bash
ettok update                    # pull, reinstall dependencies, rebuild the UI
ettok update --check            # is there anything to install?
```

On Windows, close the dashboard first. Its own executable is the file the update
has to replace, and Windows will not replace a running one. The dashboard's
Update button hits the same wall for the same reason; stopping the services makes
it work:

```
ettok serve --stop
ettok gateway stop
```

## Configuration and state

Everything the agent holds lives outside the repository, so an update never
touches it:

| | Linux / macOS | Windows |
|---|---|---|
| Config | `~/.hermes/config.yaml` | `%LOCALAPPDATA%\hermes\config.yaml` |
| Secrets | `~/.hermes/.env` | `%LOCALAPPDATA%\hermes\.env` |
| Evidence and local state | `~/.hermes/plugin-data/ettok/` | `%LOCALAPPDATA%\hermes\plugin-data\ettok\` |

Those paths keep the inherited name on purpose. Renaming them would break
compatibility with the upstream runtime for no gain a user can see.

## What it will not do

The agent treats a CAPTCHA or a checkpoint as the platform saying it has
noticed — it quarantines the account and stops, rather than solving it. Solving a
challenge removes the only warning and leaves the detection in place, and the
account escalates to a permanent ban instead of backing off while it is still
recoverable.

It does not decide its own limits either. Deadlines, budgets and stop conditions
are enforced by the platform, and a case past any of them is never sent.

## Built on Hermes Agent

Ettok AI is built on [Hermes Agent](https://github.com/NousResearch/hermes-agent)
by Nous Research, used under the MIT Licence — see
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).

That project supplies the runtime: the agent loop, scheduling, local state and
search, provider adapters, browser tooling and the operator interface. None of it
is rebuilt here, which is why the engineering goes into Iraqi-specific detection
rather than into plumbing. Internal module names, environment variables and state
paths inherited from it are left unchanged so upstream fixes can still be merged.

## Licence

MIT. See [LICENSE](LICENSE) and
[THIRD-PARTY-LICENSES.md](THIRD-PARTY-LICENSES.md).
