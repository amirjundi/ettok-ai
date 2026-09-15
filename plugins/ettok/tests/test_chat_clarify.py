"""The answer has to reach a thread that is already blocked.

The dashboard turn holds one HTTP request open for its whole duration, and the
agent asking a question parks the worker thread inside that request. So the
answer cannot arrive on the same request -- nothing is reading it -- and the
only thing worth testing is that a second, independent request wakes the first.
"""

from __future__ import annotations

import json
import threading

import pytest

from tools import clarify_gateway


class _Req:
    """The two things the handler reads off an aiohttp request."""

    def __init__(self, clarify_id, body):
        self.match_info = {"clarify_id": clarify_id}
        self._body = body

    async def json(self):
        if self._body is _BAD:
            raise ValueError("not json")
        return self._body


_BAD = object()


@pytest.fixture
def handler():
    from gateway.platforms.api_server import APIServerAdapter
    return APIServerAdapter._handle_clarify_answer


@pytest.fixture
def pending():
    """A question waiting, cleaned up however the test ends."""
    entry = clarify_gateway.register(
        clarify_id="cid-1", session_key="sess-1", question="Which community?",
        choices=["Yazidi", "Assyrian"], multi_select=False)
    yield entry
    clarify_gateway.clear_session("sess-1")


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def _body(response):
    return json.loads(response.body.decode("utf-8"))


def test_an_answer_wakes_the_blocked_thread(handler, pending):
    woke = {}

    def agent_thread():
        woke["answer"] = clarify_gateway.wait_for_response("cid-1", 10)

    worker = threading.Thread(target=agent_thread, daemon=True)
    worker.start()

    response = _run(handler(None, _Req("cid-1", {"response": "Yazidi"})))
    assert response.status == 200

    worker.join(timeout=5)
    assert not worker.is_alive(), "the agent thread never came back"
    assert woke["answer"] == "Yazidi"


def test_a_multi_select_answer_arrives_as_one_string(handler, pending):
    """Checkboxes give a list; the agent gets a string either way."""
    got = {}

    def agent_thread():
        got["answer"] = clarify_gateway.wait_for_response("cid-1", 10)

    worker = threading.Thread(target=agent_thread, daemon=True)
    worker.start()
    assert _run(handler(None, _Req("cid-1", {"response": ["Yazidi", "Assyrian"]}))).status == 200
    worker.join(timeout=5)
    assert got["answer"] == "Yazidi, Assyrian"


def test_an_unknown_id_is_not_silently_accepted(handler):
    response = _run(handler(None, _Req("no-such-id", {"response": "Yazidi"})))
    assert response.status == 404


def test_an_empty_answer_is_refused(handler, pending):
    """Resolving with "" would unblock the agent with no answer at all -- worse
    than the timeout, because it looks like the user said something."""
    response = _run(handler(None, _Req("cid-1", {"response": "   "})))
    assert response.status == 400
    assert clarify_gateway.has_pending("sess-1"), "the question was discarded anyway"


def test_a_malformed_body_is_refused(handler, pending):
    response = _run(handler(None, _Req("cid-1", _BAD)))
    assert response.status == 400
    assert clarify_gateway.has_pending("sess-1")
