# -*- coding: utf-8 -*-
"""A machine remembers which platform it paired with.

`--platform` set the URL for one process. Pairing then succeeded, the key landed
in .env, and the address was forgotten when the command exited -- so the next run
fell back to the built-in default and reported connection refused against a
platform nobody had ever pointed it at.

The state that leaves is the confusing one: paired is true, the key is real, and
every call fails. The agent on the machine this happened to described itself as
"half connected", which was exactly the situation.
"""
from plugins.ettok.platform import pairing


class Result:
    agent_id = 'DESKTOP-JVR9IQO'
    agent_key = 'k' * 40


def test_the_platform_is_written_beside_the_key(tmp_path):
    env = tmp_path / '.env'

    pairing.write_credentials(Result(), env, 'http://192.168.100.44:8099')

    body = env.read_text(encoding='utf-8')
    assert 'ETTOK_AGENT_ID=DESKTOP-JVR9IQO' in body
    assert 'ETTOK_AGENT_KEY=' + 'k' * 40 in body
    assert 'ETTOK_PLATFORM_URL=http://192.168.100.44:8099' in body


def test_a_trailing_slash_is_dropped(tmp_path):
    """Endpoints are joined onto this, and a double slash is a 404 nobody
    expects to have caused."""
    env = tmp_path / '.env'

    pairing.write_credentials(Result(), env, 'http://192.168.100.44:8099/')

    assert 'ETTOK_PLATFORM_URL=http://192.168.100.44:8099\n' in env.read_text(encoding='utf-8')


def test_re_pairing_replaces_rather_than_stacks(tmp_path):
    """A stale URL above a live one is how a machine ends up talking to the
    wrong platform after being moved."""
    env = tmp_path / '.env'

    pairing.write_credentials(Result(), env, 'http://old-host:8099')
    pairing.write_credentials(Result(), env, 'http://192.168.100.44:8099')

    body = env.read_text(encoding='utf-8')
    assert body.count('ETTOK_PLATFORM_URL=') == 1
    assert 'old-host' not in body
    assert body.count('ETTOK_AGENT_KEY=') == 1


def test_other_settings_survive_pairing(tmp_path):
    """The .env holds the model key and the chat key too. Pairing must not
    take them out."""
    env = tmp_path / '.env'
    env.write_text('DEEPSEEK_API_KEY=abc\nAPI_SERVER_KEY=def\n', encoding='utf-8')

    pairing.write_credentials(Result(), env, 'http://192.168.100.44:8099')

    body = env.read_text(encoding='utf-8')
    assert 'DEEPSEEK_API_KEY=abc' in body
    assert 'API_SERVER_KEY=def' in body


def test_pairing_without_a_url_still_works(tmp_path):
    """Older call sites pass two arguments; they must not start failing."""
    env = tmp_path / '.env'

    pairing.write_credentials(Result(), env)

    body = env.read_text(encoding='utf-8')
    assert 'ETTOK_AGENT_KEY=' in body
    assert 'ETTOK_PLATFORM_URL=' not in body
