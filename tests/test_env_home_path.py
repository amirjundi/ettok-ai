# -*- coding: utf-8 -*-
"""The .env must be read from the directory the runtime calls home.

On Windows the platform default home is %LOCALAPPDATA%\\hermes, and the loader
had its own second guess at the same path -- ~/.hermes -- which nothing creates.
Every credential written to the real home was therefore invisible to anything
reading os.environ: the key the dashboard's chat authenticates with, the platform
URL, the pairing key.

The symptoms did not point at a path. The chat failed with the gateway running
(no auth header, so the gateway answered 401), and `ettok doctor` reported "not
paired" while sitting next to a .env containing the pairing key. Two separate
things that both looked like their own bug.
"""
import os
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import env_loader
from hermes_constants import get_hermes_home


@pytest.fixture(autouse=True)
def _clean_env():
    saved = {k: os.environ.get(k) for k in ('ETTOK_PROBE_KEY', 'HERMES_HOME')}
    yield
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_the_loader_looks_in_the_runtime_home(tmp_path):
    """The whole bug in one assertion.

    Everything else resolves the home through get_hermes_home(); the loader was
    the one place that did not, and on Windows the two answers differ. Asserted
    by pointing get_hermes_home() somewhere known and checking the loader
    followed it -- rather than by comparing two paths that agree on POSIX, which
    is where this would never have been caught.
    """
    (tmp_path / '.env').write_text('ETTOK_PROBE_KEY=from-runtime-home\n', encoding='utf-8')
    os.environ.pop('ETTOK_PROBE_KEY', None)

    with mock.patch.object(env_loader, 'get_hermes_home', return_value=tmp_path, create=True):
        with mock.patch('hermes_constants.get_hermes_home', return_value=tmp_path):
            env_loader.load_hermes_dotenv()
            assert os.environ.get('ETTOK_PROBE_KEY') == 'from-runtime-home'


def test_a_key_written_to_the_runtime_home_is_readable(tmp_path):
    """Write where the agent writes, read where the runtime reads."""
    env_file = tmp_path / '.env'
    env_file.write_text('ETTOK_PROBE_KEY=probe-value-123\n', encoding='utf-8')

    os.environ.pop('ETTOK_PROBE_KEY', None)
    env_loader.load_hermes_dotenv(hermes_home=tmp_path)

    assert os.environ.get('ETTOK_PROBE_KEY') == 'probe-value-123'


def test_HERMES_HOME_still_wins(tmp_path):
    """The override has to keep working: profiles and tests depend on it."""
    (tmp_path / '.env').write_text('ETTOK_PROBE_KEY=from-override\n', encoding='utf-8')

    os.environ.pop('ETTOK_PROBE_KEY', None)
    with mock.patch.dict(os.environ, {'HERMES_HOME': str(tmp_path)}):
        env_loader.load_hermes_dotenv()
        # Asserted inside the block: patch.dict restores os.environ on exit, so
        # outside it the variable the loader just set has been removed again.
        assert os.environ.get('ETTOK_PROBE_KEY') == 'from-override'


def test_an_explicit_home_argument_still_wins(tmp_path):
    (tmp_path / '.env').write_text('ETTOK_PROBE_KEY=from-argument\n', encoding='utf-8')

    os.environ.pop('ETTOK_PROBE_KEY', None)
    env_loader.load_hermes_dotenv(hermes_home=tmp_path)

    assert os.environ.get('ETTOK_PROBE_KEY') == 'from-argument'


def test_the_agent_writes_where_the_loader_reads():
    """The two halves of the round trip, named together.

    The ettok CLI resolves its .env through get_hermes_home(); the loader now
    does too. If either side ever grows its own idea of home again, this fails.
    """
    from plugins.ettok.cli import _env_path

    assert Path(_env_path()).parent == Path(get_hermes_home())
