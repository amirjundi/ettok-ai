# -*- coding: utf-8 -*-
"""A turn must finish whether or not anyone is watching it.

Relaying straight from the gateway to the browser tied the agent's work to
somebody looking at it. Closing the tab or switching pages cancelled the
response mid-sentence, and the operator came back to their own question with
nothing under it.

Measured before the fix: a forty-step answer, client disconnected after four
seconds, and the reply stored in the session was one character long.
"""
import asyncio

import httpx
import pytest

from plugins.ettok.dashboard import plugin_api as api


class FakeStream:
    """A gateway response that yields slowly, so a disconnect lands mid-answer."""

    def __init__(self, chunks, status=200, delay=0.01, seen=None):
        self._chunks = chunks
        self.status_code = status
        self.headers = {'X-Hermes-Session-Id': 'api-test'}
        self._delay = delay
        self._seen = seen if seen is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            await asyncio.sleep(self._delay)
            self._seen.append(chunk)
            yield chunk

    async def aread(self):
        return b'error body'


class FakeClient:
    def __init__(self, stream):
        self._stream = stream

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *a, **kw):
        return self._stream


@pytest.fixture(autouse=True)
def _clear_tasks():
    api._running_turns.clear()
    yield
    api._running_turns.clear()


def _request(body=None):
    """`chat` takes the parsed body, not a Request -- FastAPI does the parsing."""
    return body or {'messages': [{'role': 'user', 'content': 'hi'}]}


@pytest.mark.asyncio
async def test_the_whole_answer_is_read_even_after_the_reader_leaves(monkeypatch):
    """The regression, stated directly: the gateway is drained to the end."""
    chunks = [f'data: chunk-{i}\n\n'.encode() for i in range(20)]
    seen = []
    stream = FakeStream(chunks, seen=seen)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: FakeClient(stream))

    response = await api.chat(_request())

    # Read two chunks, then walk away exactly as a closed tab does.
    iterator = response.body_iterator.__aiter__()
    await iterator.__anext__()
    await iterator.__anext__()
    await iterator.aclose()

    # The turn keeps going without a reader.
    for _ in range(200):
        await asyncio.sleep(0.01)
        if len(seen) == len(chunks):
            break

    assert len(seen) == len(chunks), (
        f'the gateway was abandoned after {len(seen)} of {len(chunks)} chunks'
    )


@pytest.mark.asyncio
async def test_a_watched_turn_still_streams_everything(monkeypatch):
    """The ordinary case must not regress into buffering or dropping."""
    chunks = [f'data: chunk-{i}\n\n'.encode() for i in range(5)]
    stream = FakeStream(chunks)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: FakeClient(stream))

    response = await api.chat(_request())
    received = [chunk async for chunk in response.body_iterator]

    # The session announcement, then every chunk.
    assert any(b'api-test' in chunk for chunk in received)
    for chunk in chunks:
        assert chunk in received


@pytest.mark.asyncio
async def test_the_running_task_is_held(monkeypatch):
    """asyncio keeps only a weak reference to a task, and a turn nobody is
    watching is exactly the one that would be collected mid-sentence."""
    stream = FakeStream([b'data: x\n\n'], delay=0.05)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: FakeClient(stream))

    await api.chat(_request())
    assert api._running_turns, 'the turn was not held anywhere'


@pytest.mark.asyncio
async def test_a_gateway_error_reaches_the_reader(monkeypatch):
    stream = FakeStream([], status=502)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: FakeClient(stream))

    response = await api.chat(_request())
    body = b''.join([chunk async for chunk in response.body_iterator])

    assert b'gateway returned 502' in body


@pytest.mark.asyncio
async def test_a_session_is_reported_active_while_its_turn_runs(monkeypatch):
    """The gap behind "it never replied".

    A turn is only written into the session when it finishes; while it runs the
    rows are tool calls and assistant entries with no text, which the chat page
    correctly drops. So an operator returning mid-turn saw their own message and
    nothing under it. This is how the page learns to say "still working".
    """
    chunks = [f'data: chunk-{i}\n\n'.encode() for i in range(10)]
    stream = FakeStream(chunks, delay=0.05)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: FakeClient(stream))

    api._active_sessions.clear()
    response = await api.chat(_request())

    iterator = response.body_iterator.__aiter__()
    await iterator.__anext__()          # the session announcement
    await iterator.__anext__()
    await iterator.aclose()             # the reader walks away

    await asyncio.sleep(0.1)
    active = [row['session_id'] for row in api.chat_active()['sessions']]
    assert 'api-test' in active, 'a running turn was not reported as active'

    # And it stops being reported once the turn lands.
    for _ in range(200):
        await asyncio.sleep(0.02)
        if not api.chat_active()['sessions']:
            break
    assert api.chat_active()['sessions'] == [], 'a finished turn is still reported running'


@pytest.mark.asyncio
async def test_a_failed_turn_does_not_stay_marked_active(monkeypatch):
    """An indicator that spins for ever is worse than one that stops."""
    stream = FakeStream([], status=502)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda *a, **kw: FakeClient(stream))

    api._active_sessions.clear()
    response = await api.chat(_request())
    [chunk async for chunk in response.body_iterator]

    assert api.chat_active()['sessions'] == []
