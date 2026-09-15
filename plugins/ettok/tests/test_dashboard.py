"""The dashboard is two plugins, and the split is the thing worth asserting.

A dashboard plugin gets exactly one tab. `ettok` uses its tab for the monitoring
panels; `ettok-chat` uses its own to override `/chat`, which is how the host's
xterm terminal page is replaced rather than merely sat next to.

The chat bundle's two parsers -- the markdown renderer and the SSE frame reader
-- have real logic and a real edge each: one builds HTML from model output, the
other decides whether a tool call is visible at all. Both are checked under Node
rather than trusted.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).parent
PLUGINS = HERE.parent.parent
PANEL = PLUGINS / 'ettok' / 'dashboard'
CHAT = PLUGINS / 'ettok-chat' / 'dashboard'
REPO = PLUGINS.parent


def test_panel_owns_its_own_tab():
    manifest = json.loads((PANEL / 'manifest.json').read_text(encoding='utf-8'))
    assert manifest['tab']['path'] == '/ettok'
    assert 'override' not in manifest['tab']
    source = (PANEL / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert '__HERMES_PLUGINS__.register("ettok"' in source


def test_chat_replaces_the_built_in_terminal():
    manifest = json.loads((CHAT / 'manifest.json').read_text(encoding='utf-8'))
    # Without this the host mounts its PTY terminal page and ours sits beside it.
    assert manifest['tab']['override'] == '/chat'
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert '__HERMES_PLUGINS__.register("ettok-chat"' in source
    # It has no backend of its own: the proxy lives with the agent's other local
    # state, in the ettok plugin.
    assert 'api' not in manifest
    assert '"/api/plugins/ettok"' in source


@pytest.mark.skipif(shutil.which('node') is None, reason='Node not installed')
def test_chat_bundle_parsers():
    result = subprocess.run(
        [shutil.which('node'), str(HERE / 'bundle_check.js')],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which('node') is None, reason='Node not installed')
def test_clarify_prompt_refuses_to_collect_a_secret():
    """A clarify prompt carries the agent's authority, which makes it a far more
    convincing place to ask for a password than a chat bubble. The agent is
    forbidden to ask, so a question like that means something steered it."""
    result = subprocess.run(
        [shutil.which('node'), str(HERE / 'secret_guard_check.js')],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=60,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def test_panel_offers_goals():
    """A goal is a cron job, not a timer of our own.

    cron survives restarts, reboots and a closed laptop; an in-process loop does
    not, and an agent whose whole purpose is working unattended on someone
    else's machine lives or dies on that difference. Asserting the panel talks
    to the cron API keeps a future "simpler" rewrite from quietly regressing it.
    """
    source = (PANEL / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert '/api/cron/jobs' in source
    assert 'ettok:working-a-case' in source          # goals load the skill
    assert "enabled_toolsets" in source


def test_chat_has_the_controls_an_operator_needs():
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    # Conversations survive a reload: without the resume id every message would
    # start a new session, which reads as the agent forgetting.
    assert 'resume_session_id' in source
    assert '/api/sessions' in source
    # Context pressure is visible before the agent silently compresses.
    assert 'effective_context_length' in source
    # Attachments and effort are gated on what the model actually supports.
    assert 'supports_vision' in source
    assert 'reasoning_effort' in source


def test_bundles_follow_the_dashboard_font():
    """Fonts come from the theme, so the plugins never fight the font picker."""
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert '--theme-font-mono' in source
    assert 'ui-monospace,Menlo,monospace' not in source


@pytest.mark.skipif(shutil.which('node') is None, reason='Node not installed')
def test_pages_actually_render():
    """Mount both pages in a DOM and let their effects run.

    A parse check says nothing about a page that references a variable which
    does not exist or crashes once its data arrives -- and that reaches a user
    as a blank tab. Skipped rather than failed when the web dependencies are
    absent: this is a repo with a built frontend, not a JS project, and a
    Python-only checkout should not fail for want of React.
    """
    result = subprocess.run(
        [shutil.which('node'), str(HERE / 'render_check.js')],
        capture_output=True, text=True, encoding='utf-8', errors='replace',
        timeout=120, cwd=str(PLUGINS.parent),
    )
    output = (result.stderr or '') + (result.stdout or '')
    if result.returncode != 0 and 'Cannot find module' in output:
        pytest.skip('react/jsdom not installed in this checkout')
    assert result.returncode == 0, output


def test_context_meter_is_not_fed_cumulative_spend():
    """The meter shows how full the window is, not what the turn cost.

    These are different numbers and conflating them produced a visible
    impossibility: 1.2M / 1.0M. `usage` from the gateway is the agent's,
    summed across every model call in a turn -- a reply that ran three shell
    commands reports ~28k prompt tokens against a context that never exceeded
    ~15k, because each tool round-trip resends the conversation. Over a long
    agentic session that sum sails past the window.

    Occupancy is only knowable from the stored per-message token counts, so the
    meter reads those and `usage` is shown separately, named as spend.
    """
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert 'setTurnSpend(obj.usage.total_tokens)' in source, \
        'usage must feed the spend readout'
    assert 'setUsedTokens(obj.usage' not in source, \
        'usage must never feed the context meter'
    # Occupancy is ESTIMATED from the message text, not read from token_count.
    #
    # The runtime declares that column and never writes it -- every row on a
    # live install is NULL -- so summing it produced a meter that read zero for
    # ever. A meter stuck at zero is worse than none: it reports "plenty of
    # room" with the same confidence whatever is true, right up to the
    # compaction nobody was warned about.
    #
    # An estimate can be wrong; it cannot be confidently wrong in one direction,
    # and it is labelled with a "≈" so the reader knows which kind of number it
    # is.
    assert 'estimateTokens' in source, 'occupancy must be measured from the text'
    # Not read anywhere. The comment explaining why it is not read stays.
    assert '.token_count' not in source,         'token_count is never written by the runtime; reading it yields a meter stuck at zero'
    assert '"≈"' in source, 'an estimate must be presented as one'
    # It is counted in both places a conversation changes size: when one is
    # opened, and when a turn completes.
    assert source.count('recount(') >= 2


def test_chat_lists_every_channel_not_just_its_own():
    """The sidebar asked for source=api_server, so a Telegram conversation with
    the same agent, in the same database, was invisible on the dashboard."""
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert 'source=api_server' not in source, 'the sidebar is back to one channel'
    assert 'exclude_sources=cron' in source, 'scheduled runs belong on their own page'
    for channel in ('telegram', 'whatsapp', 'discord', 'signal'):
        assert '"%s"' % channel in source, '%s has no channel entry' % channel


def test_other_channels_are_read_only():
    """A reply typed here returns over this page's stream. It does not reach the
    Telegram thread it appears to answer, so the composer must not offer to."""
    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert 'readOnly' in source
    assert 'sessionChannel !== "api_server"' in source, 'nothing decides what is answerable'
    assert 'read only' in source.lower(), 'the page never says why it cannot reply'


def test_agent_can_ask_the_user_a_question():
    """clarify had no callback on this surface: the tool returned "unavailable"
    and the agent guessed instead of asking."""
    routes = (REPO / 'gateway' / 'platforms' / 'api_server.py').read_text(encoding='utf-8')
    assert '/v1/clarify/{clarify_id}' in routes, 'no route to answer on'
    assert 'agent.clarify_callback = clarify_callback' in routes

    stream = (REPO / 'gateway' / 'platforms' / 'api_server_openai_routes.py').read_text(encoding='utf-8')
    assert 'clarify_callback=_on_clarify' in stream, 'the callback is never wired to the turn'
    assert '"hermes.clarify"' in stream, 'the question never reaches the wire'
    assert 'wait_for_response' in stream, 'the agent does not wait for the answer'

    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert 'hermes.clarify' in source, 'the page ignores the question'
    assert '/chat/clarify' in source, 'the page has no way to answer'
    # A pending question blocks the agent, so a typed answer must not queue
    # behind the turn that is waiting for it.
    assert 'if (ask) {' in source
    assert 'if (busy || ask || !queued.length) return;' in source


def test_shell_output_is_shown_not_discarded():
    """"Running npm test" with the output dropped is the agent working where
    nobody can see it."""
    stream = (REPO / 'gateway' / 'platforms' / 'api_server_openai_routes.py').read_text(encoding='utf-8')
    assert '_OUTPUT_TOOLS' in stream and 'terminal' in stream
    assert 'frame["output"] = _clip_output(function_result)' in stream

    source = (CHAT / 'dist' / 'index.js').read_text(encoding='utf-8')
    assert 'ShellOutput' in source
    assert 'SHELL_TOOLS' in source
