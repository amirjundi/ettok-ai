# -*- coding: utf-8 -*-
"""Posting to social accounts from the machine that is signed in to them.

The platform has been queueing this work since the day it shipped and nothing
ever collected it. A social account set to "a computer you run posts it from a
signed-in browser" creates an `AgentJob` of kind `social_publish` and waits;
there was no agent on the other end, so those jobs sat pending for ever.

Why it lives here rather than on the server: the server has no browser and no
signed-in session, and the accounts that matter have no usable publishing API.
The credential stays on one machine, in the agent's own vault, and is filled
into the page there. Nothing about it reaches the platform.

The same rule as collection applies, for the same reason. A checkpoint, a
captcha or a re-login prompt is the platform noticing the automation -- it is a
detection signal, not an obstacle. The account is quarantined and the job goes
back for a person. Working around it, or asking somebody to clear a challenge
that this agent's own request triggered, is how an account is lost for good and
how a newsroom's distribution disappears with it.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

KIND = 'social_publish'


def claim(client, limit: int = 5) -> list:
    """Ask the platform for work. Returns the jobs this machine now owns."""
    reply = client.claim_jobs(KIND, limit=limit)
    jobs = (reply or {}).get('jobs') or []
    logger.info('Claimed %s social job(s)', len(jobs))
    return jobs


def report(client, job_id: int, status: str, result: Optional[dict] = None) -> None:
    """Say what happened, always.

    A claimed job that is never reported is worse than one that failed: the
    platform believes a machine is working on it, so nobody picks it up and the
    article is never distributed. Every path out of `handle` reports.
    """
    client.report_job(job_id, status, result or {})


def describe(job: dict) -> str:
    """One line for a log or a panel, without the caption's whole text."""
    payload = job.get('payload') or {}
    caption = (payload.get('caption') or '').strip().replace('\n', ' ')
    return '{} to {} -- {}'.format(
        job.get('id'), payload.get('platform', '?'),
        (caption[:60] + '...') if len(caption) > 60 else caption or '(no caption)')


def blocked_by_platform(page_text: str):
    """The collection rule, reused rather than restated.

    A second copy of these patterns would drift from the first, and the half
    that drifts is the half that stops recognising a checkpoint.
    """
    from .collect.session import detect_block

    return detect_block(page_text or '')


def handle(conn, client, job: dict, *, post) -> dict:
    """Work one job: post it, or hand it back with a reason.

    `post` is supplied by the caller -- in production the agent's browser, in a
    test a stand-in -- because this module's job is the contract with the
    platform, not the mechanics of one social network's composer.
    """
    from .collect import session as session_mod

    job_id = job.get('id')
    payload = job.get('payload') or {}
    account_id = str(payload.get('account_id') or '')

    state = session_mod.account_state(conn, account_id) if account_id else 'healthy'
    if state in ('quarantined', 'auth_lost'):
        # Not an error and not a retry: this account is not usable, and saying
        # so returns the post to the manual queue where a person can send it.
        report(client, job_id, 'failed',
               {'reason': 'account {} is {}'.format(account_id, state)})
        return {'job': job_id, 'outcome': 'account_unavailable', 'state': state}

    try:
        result = post(payload)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning('Posting job %s failed: %s', job_id, exc)
        report(client, job_id, 'failed', {'reason': str(exc)[:300]})
        return {'job': job_id, 'outcome': 'failed', 'error': str(exc)}

    blocked = blocked_by_platform((result or {}).get('page_text', ''))
    if blocked is not None:
        # The platform noticed. Stop using this account, and do not try again
        # from somewhere else -- that is the same automation wearing a hat.
        if account_id:
            session_mod.quarantine(conn, account_id, str(blocked),
                                   auth_lost=getattr(blocked, 'auth_lost', False))
        report(client, job_id, 'failed',
               {'reason': 'blocked: {}'.format(blocked),
                'account_quarantined': account_id})
        return {'job': job_id, 'outcome': 'blocked', 'reason': str(blocked)}

    if not (result or {}).get('posted'):
        report(client, job_id, 'failed',
               {'reason': (result or {}).get('reason') or 'the post was not confirmed'})
        return {'job': job_id, 'outcome': 'not_confirmed'}

    if account_id:
        session_mod.record_success(conn, account_id)
    report(client, job_id, 'done',
           {'url': (result or {}).get('url', ''), 'posted': True})
    return {'job': job_id, 'outcome': 'posted', 'url': (result or {}).get('url', '')}


def run(ctx, *, post, limit: int = 5) -> dict:
    """Claim what is waiting and work it. Returns a summary rather than raising.

    One bad job must not strand the ones behind it, which is the same reason the
    collection outbox exists.
    """
    from . import config as config_mod
    from .platform.client import PlatformClient
    from .store import schema

    cfg = config_mod.load(ctx)
    conn = schema.connect()
    client = PlatformClient(cfg)

    summary = {'claimed': 0, 'posted': 0, 'failed': 0, 'blocked': 0, 'jobs': []}

    for job in claim(client, limit=limit):
        summary['claimed'] += 1
        outcome = handle(conn, client, job, post=post)
        summary['jobs'].append(outcome)
        if outcome['outcome'] == 'posted':
            summary['posted'] += 1
        elif outcome['outcome'] == 'blocked':
            summary['blocked'] += 1
        else:
            summary['failed'] += 1

    if summary['blocked']:
        summary['note'] = (
            'An account was quarantined because the platform showed a challenge. '
            'That is a detection signal, not an obstacle: do not clear it and do '
            'not retry from another machine. A person signs in themselves, when '
            'they choose to.'
        )
    return summary
