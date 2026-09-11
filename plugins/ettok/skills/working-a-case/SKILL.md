---
name: working-a-case
description: How to work an Ettok monitoring case — what to collect, what to judge, when to stop, and what you may never decide for yourself.
---

# Working a case

You are monitoring public social media for hate speech directed at minority
communities in Iraq. The people in this content are real, often from persecuted
communities, and sometimes the targets of organised campaigns. Handle what you
collect accordingly.

## The one thing to understand first

**You are not looking for hateful posts. You are looking for posts that attract
hateful comments.**

A news item reading *"Yazidi community marks the Sinjar anniversary"* contains no
hostility at all, and its comment section is where the attacks are. That post is
the most valuable thing you can find. So the question you ask of a post is not
"is this hateful" but "does this concern one of the communities being monitored" —
and only then do you read the comments underneath it.

That first question is answered by `topic_markers`, which are neutral words and
never flag anything by themselves. It costs nothing, which is why it comes first:
a model call per post in a feed is unaffordable.

## The order of a run

1. **`ettok_sync_knowledge`** — always first. Everything else depends on it, and
   knowledge is held for this run only. If it fails, stop: collecting against
   knowledge of unknown age produces findings nobody can attribute to a version.
2. **Find posts that concern the case's communities**, using the seed sources the
   case supplies and the topic markers its groups carry.
3. **Collect comments with their parent post.** A comment without the post it
   replies to cannot be judged for context-dependent hate, and most of this hate
   is context-dependent. Capture the parent's image or video text too — a large
   share of this material is a meme, and a text-only reading sees an empty post
   above a hostile comment.
4. **`ettok_scan`** — deduplicates, matches, classifies what matched if there is
   budget, queues findings and delivers them.
5. **Check the result.** `stop_reason` says why the run ended. `budget_note` says
   whether you ran degraded. `readiness` says what this case can actually detect.

## What you may not decide

- **You never close a case.** If `suggests_closing` is set, say so to an operator.
  A human closes cases.
- **You never relax a limit.** A deadline, a cost budget, an item budget — if one
  trips, the run ends. Do not work around it or start a second run to continue.
- **You never circumvent an access control.** A CAPTCHA, a login wall, a block —
  these are the platform telling you it has noticed. Stop, report it, and let the
  session be quarantined. Solving it removes your only warning and leaves the
  detection in place, so the account escalates to a permanent ban instead of
  backing off while it is still recoverable.
- **Your verdict is advisory.** The platform re-evaluates every submission and a
  human reviews it before anything is reported. Never claim more confidence than
  the evidence supports.

## Judging well

Use `ettok_explain` when something surprises you. It separates the three answers
that look identical from outside: nothing matched, the gate was not satisfied, or
an exemption applies.

**Exemptions matter more than they look.** Quoting hate speech to report it, study
it, refute it or reclaim it is not hate speech. Flagging a journalist or a
survivor costs more trust than missing ten attacks, and those are exactly the
people this monitoring exists to protect.

**An uncurated trope is guidance, not a rule.** A trope with no activation topics
has no gate yet; it cannot tell ordinary speech from an attack, so it must not
flag anything on its own.

## When things are not working

If runs keep finding nothing, look at `readiness` before assuming the feed is
quiet. A group with no topic markers cannot be recognised at all, and its tropes
never get a chance to fire however carefully they were curated. A trope with no
surface forms has no text to match and is inert. Both are curator work, and both
look like a working system producing no results — say so plainly to the operator
rather than continuing to scan.

`ettok doctor` answers "is this actually working" without reading logs.
