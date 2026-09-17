"""HTTP client for the Ettok platform.

The whole retry policy exists because of where this runs: an operator PC on a
residential connection that drops. A timeout there is indistinguishable from a lost
response, so a retried submission without an idempotency key is written twice, and
every rate computed from it is then wrong in a direction nobody would notice.

The status-code split is the other half. `400`, `401` and `403` are decisions the
platform has made -- a malformed body, a revoked key, a key without the scope --
and retrying them is noise, not resilience. `409`, any `5xx`, and connection
failures are the line being unreliable, and those are worth waiting out.
"""

from __future__ import annotations

import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional

import httpx

log = logging.getLogger(__name__)

# The platform says a 200 means the JSON parsed, not that the fields were
# understood. Nothing here may treat success as field-level acceptance.
PERMANENT_STATUSES = frozenset({400, 401, 403})
TRANSIENT_STATUSES = frozenset({409, 429, 500, 502, 503, 504})

MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0
BACKOFF_CAP_SECONDS = 60.0


class PlatformError(RuntimeError):
    """Base for anything that stopped a request completing."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class PermanentError(PlatformError):
    """Stop and alert an operator. Retrying cannot change this outcome.

    A revoked key retried on a loop looks like activity and achieves nothing, and
    the run that produced the data is over by the time anyone notices.
    """


class TransientError(PlatformError):
    """The line, not the decision. Worth retrying with backoff."""


class NotPairedError(PermanentError):
    """No agent key on this machine. `ettok connect` is how one is obtained."""


@dataclass
class Response:
    status: int
    data: dict
    replayed: bool = False


def backoff_seconds(attempt: int) -> float:
    """Exponential with jitter, capped.

    Jitter matters more than it looks: several agents that lost the same VPS come
    back in step without it, and arrive together the moment it recovers.
    """
    raw = min(BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1)), BACKOFF_CAP_SECONDS)
    return raw * (0.5 + random.random() / 2)


class PlatformClient:
    """Authenticated access to one Ettok platform."""

    def __init__(self, config, *, transport: Any = None):
        self._config = config
        self._transport = transport

    # -- plumbing ---------------------------------------------------------

    def _headers(self, idempotency_key: Optional[str] = None,
                 *, content_type: Optional[str] = 'application/json') -> dict:
        if not self._config.is_paired:
            raise NotPairedError(
                'This machine is not paired with a platform. Run `ettok connect`.'
            )
        headers = {
            'Authorization': f'Bearer {self._config.agent_key}',
            'X-Agent-Id': self._config.agent_id,
            'Accept': 'application/json',
        }
        if content_type:
            headers['Content-Type'] = content_type
        # A multipart upload passes content_type=None on purpose: httpx writes
        # the header itself, with the boundary it generated. Setting it here
        # would hand the server a boundary that does not match the body.
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        return headers

    def _client(self) -> httpx.Client:
        kwargs: dict = {'timeout': self._config.timeout_seconds}
        if self._transport is not None:
            kwargs['transport'] = self._transport
        return httpx.Client(**kwargs)

    @staticmethod
    def _classify(response: httpx.Response) -> None:
        status = response.status_code
        if status in PERMANENT_STATUSES:
            reason = {
                400: 'the platform could not parse the request body',
                401: 'the agent key is missing, unknown or revoked',
                403: 'the agent key lacks the hate_speech_scan scope',
            }[status]
            raise PermanentError(f'{status}: {reason}', status=status)
        if status in TRANSIENT_STATUSES or status >= 500:
            raise TransientError(f'{status}: platform temporarily unavailable', status=status)
        if status >= 400:
            raise PermanentError(f'{status}: unexpected response', status=status)

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        data: Optional[Mapping[str, Any]] = None,
        files: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        attempts: int = MAX_ATTEMPTS,
        sleep=time.sleep,
    ) -> Response:
        """One request, retried on transient failures only.

        `idempotency_key` must be stable across retries of the *same* submission --
        that is the entire point of it. Generating a fresh one per attempt would
        turn one finding into five.
        """
        url = self._config.api(path)
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
        last: Optional[PlatformError] = None

        # A multipart request carries its own encoding, so the JSON body and the
        # JSON content type both step aside.
        multipart = files is not None or data is not None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                with self._client() as client:
                    response = client.request(
                        method, url,
                        content=None if multipart else body,
                        data=dict(data or {}) if multipart else None,
                        files=dict(files) if files else None,
                        params=dict(params or {}),
                        headers=self._headers(
                            idempotency_key,
                            content_type=None if multipart else 'application/json',
                        ),
                    )
                self._classify(response)
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                return Response(
                    status=response.status_code,
                    data=data if isinstance(data, dict) else {'data': data},
                    replayed=response.headers.get('Idempotency-Replayed') == 'true',
                )

            except PermanentError:
                raise
            except TransientError as exc:
                last = exc
            except httpx.HTTPError as exc:
                # A dropped connection is the expected case here, not an anomaly.
                last = TransientError(f'connection failed: {exc}')

            if attempt < attempts:
                delay = backoff_seconds(attempt)
                log.warning('ettok: %s %s failed (%s); retrying in %.1fs', method, path, last, delay)
                sleep(delay)

        raise last or TransientError('request failed with no further detail')

    # -- the contract -----------------------------------------------------

    def heartbeat(self, status: Mapping[str, Any]) -> dict:
        """Announce liveness and carry what this agent is doing.

        Status is not decoration. An agent running unattended on a machine in
        another room is only observable through this call, and `scan_requested`
        comes back through it -- one-shot, cleared by the platform when handed
        over, so it must be acted on or it is lost.
        """
        payload = {'agent_id': self._config.agent_id, **dict(status)}
        return self.request('POST', 'heartbeat/', payload=payload).data

    def tasks(self) -> dict:
        return self.request('GET', 'tasks/').data

    def lexicon(self, languages: Optional[list] = None) -> dict:
        params = [('language', lang) for lang in (languages or [])]
        return self.request('GET', 'lexicon/', params=dict(params) if params else None).data

    def tropes(self, target_group: Optional[str] = None) -> dict:
        params = {'target_group': target_group} if target_group else None
        return self.request('GET', 'tropes/', params=params).data

    def accounts(self) -> dict:
        return self.request('GET', 'accounts/').data

    def reports(self, *, limit: int = 20) -> dict:
        """What became of what was submitted.

        Read-only and advisory in the other direction: the platform's verdicts
        are shown to an operator, never fed back into the agent's own judgement.
        """
        return self.request('GET', 'reports/', params={'limit': limit}).data

    def submit_flagged(self, items: list, *, idempotency_key: str) -> Response:
        return self.request(
            'POST', 'flagged-items/', payload={'items': items}, idempotency_key=idempotency_key,
        )

    def submit_gaps(self, gaps: list, *, idempotency_key: str) -> Response:
        return self.request(
            'POST', 'lexicon-gaps/', payload={'gaps': gaps}, idempotency_key=idempotency_key,
        )

    def submit_scan_log(self, log_payload: Mapping[str, Any], *, idempotency_key: str) -> Response:
        return self.request(
            'POST', 'scan-log/', payload=dict(log_payload), idempotency_key=idempotency_key,
        )

    def save_cookies(self, accounts: list, *, idempotency_key: str) -> Response:
        return self.request(
            'POST', 'cookies/', payload={'accounts': accounts}, idempotency_key=idempotency_key,
        )

    def upload_evidence(self, *, page_hash: str, source_url: str, captured_at: str,
                        item_content_hash: str = '', case_id=None,
                        screenshot: Optional[bytes] = None,
                        archive: Optional[bytes] = None) -> Response:
        """Send one capture to the platform.

        Multipart rather than JSON: a screenshot base64'd into a JSON body is a
        third larger, on a residential uplink that is already the slowest part
        of a run. Retries are safe -- the platform keys on `page_hash` and
        confirms an upload it already holds rather than storing it twice, which
        is what lets this machine delete its own copy afterwards.
        """
        files = {}
        if screenshot:
            files['screenshot'] = ('screenshot.png', screenshot, 'image/png')
        if archive:
            files['archive'] = ('archive.html', archive, 'text/html')
        if not files:
            raise PermanentError('nothing to upload: no artefact was captured')

        data = {
            'page_hash': page_hash,
            'source_url': source_url,
            'captured_at': captured_at,
            'item_content_hash': item_content_hash,
        }
        if case_id is not None:
            data['case_id'] = str(case_id)
        return self.request('POST', 'evidence/', data=data, files=files)


def new_idempotency_key() -> str:
    return str(uuid.uuid4())
