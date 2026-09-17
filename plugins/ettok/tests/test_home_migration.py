"""Moving the state directory out of the upstream project's name.

The directory is called `hermes` after the project this was forked from, and an
operator handed software called Ettok finds another product's name in their home
folder. Around a hundred places in this codebase build that path themselves
rather than asking the resolver, so the name cannot simply be changed in code --
the move happens on disk, once, and leaves a link behind.

What is asserted here is the part that must never go wrong: nothing is lost, and
the machine is never left in a state where the directory has moved and the paths
that name the old one resolve to nothing.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hermes_cli import home_migration


@pytest.fixture
def fake_home(monkeypatch):
    """A throwaway pair of directories standing in for the real ones."""
    base = Path(tempfile.mkdtemp())
    current, target = base / 'hermes', base / 'ettok'
    monkeypatch.setattr(home_migration, 'paths', lambda: (current, target))
    # Nothing is running in a test, and psutil would see the pytest process.
    monkeypatch.setattr(home_migration, '_running', lambda: [])
    return current, target


def _populate(path: Path):
    (path / 'plugin-data' / 'ettok').mkdir(parents=True)
    (path / 'plugin-data' / 'ettok' / 'ettok.db').write_text('collected evidence')
    (path / '.env').write_text('ETTOK_PLATFORM_URL=https://ettok.net')
    return path


class TestItMovesWithoutLosingAnything:
    def test_the_state_arrives_at_the_new_name(self, fake_home):
        current, target = fake_home
        _populate(current)

        ok, message = home_migration.migrate()

        assert ok, message
        assert (target / 'plugin-data' / 'ettok' / 'ettok.db').read_text() == 'collected evidence'
        assert (target / '.env').exists()

    def test_the_old_path_still_resolves(self, fake_home):
        """About a hundred places in this codebase name it. Leaving them pointing
        at nothing is worse than never having run this."""
        current, target = fake_home
        _populate(current)

        home_migration.migrate()

        assert current.exists()
        assert (current / '.env').read_text() == 'ETTOK_PLATFORM_URL=https://ettok.net'
        assert (current / 'plugin-data' / 'ettok' / 'ettok.db').exists()

    def test_nothing_is_duplicated(self, fake_home):
        """Moved, not copied then deleted: there is never a window with two
        divergent copies of the evidence."""
        current, target = fake_home
        _populate(current)

        home_migration.migrate()

        assert home_migration._is_link(current)
        assert not home_migration._is_link(target)


class TestItRefusesRatherThanGuess:
    def test_it_will_not_run_while_something_is_live(self, fake_home, monkeypatch):
        """Moving the directory out from under a live gateway or an open SQLite
        database corrupts both."""
        current, target = fake_home
        _populate(current)
        monkeypatch.setattr(home_migration, '_running', lambda: ['ettok gateway (123)'])

        ok, message = home_migration.migrate()

        assert not ok
        assert 'ettok gateway' in message
        assert not target.exists(), 'nothing was moved'

    def test_an_unknown_process_state_is_treated_as_running(self, fake_home, monkeypatch):
        """Without psutil this cannot be checked, and a migration that assumes
        "probably nothing" is how a database gets corrupted."""
        current, target = fake_home
        _populate(current)
        monkeypatch.setattr(home_migration, '_running', lambda: ['unknown'])

        ok, _message = home_migration.migrate()

        assert not ok
        assert not target.exists()

    def test_it_will_not_merge_two_state_directories(self, fake_home):
        current, target = fake_home
        _populate(current)
        target.mkdir()
        (target / 'something-else').write_text('from another install')

        ok, message = home_migration.migrate()

        assert not ok
        assert 'will not merge' in message
        assert (current / '.env').exists(), 'the original is untouched'


class TestItIsSafeToRunTwice:
    def test_a_second_run_changes_nothing(self, fake_home):
        current, target = fake_home
        _populate(current)

        first_ok, _ = home_migration.migrate()
        second_ok, second_message = home_migration.migrate()

        assert first_ok
        assert second_ok
        assert 'Already done' in second_message
        assert (target / '.env').exists()

    def test_a_machine_with_nothing_to_move_is_not_an_error(self, fake_home):
        _current, target = fake_home

        ok, message = home_migration.migrate()

        assert ok
        assert 'Nothing to move' in message
        assert not target.exists()


class TestTheResolverPrefersWhatIsOnDisk:
    """Nothing is renamed in code, so an install made before this keeps working
    exactly as it did, and only a machine that ran the migration moves."""

    def test_an_existing_legacy_install_is_still_used(self, tmp_path, monkeypatch):
        import hermes_constants

        monkeypatch.setattr(hermes_constants.sys, 'platform', 'linux')
        monkeypatch.setattr(hermes_constants.Path, 'home', staticmethod(lambda: tmp_path))
        (tmp_path / '.hermes').mkdir()

        assert hermes_constants._get_platform_default_hermes_home().name == '.hermes'

    def test_a_migrated_install_is_used_when_both_exist(self, tmp_path, monkeypatch):
        """After a migration the legacy path is a link to the new one, so both
        appear to exist. The migrated one has to win."""
        import hermes_constants

        monkeypatch.setattr(hermes_constants.sys, 'platform', 'linux')
        monkeypatch.setattr(hermes_constants.Path, 'home', staticmethod(lambda: tmp_path))
        (tmp_path / '.hermes').mkdir()
        (tmp_path / '.ettok').mkdir()

        assert hermes_constants._get_platform_default_hermes_home().name == '.ettok'

    def test_a_fresh_machine_starts_in_the_new_name(self, tmp_path, monkeypatch):
        import hermes_constants

        monkeypatch.setattr(hermes_constants.sys, 'platform', 'linux')
        monkeypatch.setattr(hermes_constants.Path, 'home', staticmethod(lambda: tmp_path))

        assert hermes_constants._get_platform_default_hermes_home().name == '.ettok'
