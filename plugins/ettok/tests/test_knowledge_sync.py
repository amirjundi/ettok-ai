"""Where a run's knowledge comes from, and what it does when it cannot get it.

`fetch` had no tests. It is the function that decides what every finding in a
run is judged against, and it was changed here for ARCH-03 -- terms, tropes and
cases now come from one endpoint that reads them at a single instant, instead
of three requests a curator's edit could land between.

The fallback matters as much as the new path. The platform and this agent are
released separately, and a sync that refused to run against a server one
version behind would be a worse failure than the one being fixed.

Run with:  .venv/Scripts/python -m pytest plugins/ettok/tests -q
"""

from __future__ import annotations

import pytest

from plugins.ettok.platform import knowledge as knowledge_mod
from plugins.ettok.platform.knowledge import KnowledgeError

TERM = {'id': 1, 'term': 'x', 'target_group_slug': 'yazidi'}
TROPE = {'id': 2, 'name': 't', 'target_groups': ['yazidi']}
CASE = {'id': 3, 'title': 'Sinjar watch', 'state': 'active'}


class FakeClient:
    """A platform that answers, with the bundle endpoint present or missing."""

    def __init__(self, *, has_bundle=True, fail=None):
        self.has_bundle = has_bundle
        self.fail = fail
        self.calls = []

    def _note(self, name):
        self.calls.append(name)
        if self.fail == name:
            raise RuntimeError('the server said no')

    def heartbeat(self, _status):
        self._note('heartbeat')
        return {'config': {'scan_interval': 30}}

    def accounts(self):
        self._note('accounts')
        return {'accounts': [{'platform': 'facebook'}]}

    def bundle(self, languages=None):
        self._note('bundle')
        if not self.has_bundle:
            raise RuntimeError('404 Not Found')
        return {'terms': [TERM], 'tropes': [TROPE], 'cases': [CASE]}

    def tasks(self):
        self._note('tasks')
        return {'cases': [CASE]}

    def lexicon(self, languages=None):
        self._note('lexicon')
        return {'terms': [TERM], 'tropes': []}

    def tropes(self):
        self._note('tropes')
        return {'tropes': [TROPE]}


class TestTheBundlePath:
    def test_it_is_preferred(self):
        client = FakeClient()
        know = knowledge_mod.fetch(client)
        assert 'bundle' in client.calls
        assert 'lexicon' not in client.calls

    def test_everything_arrives(self):
        know = knowledge_mod.fetch(FakeClient())
        assert know.terms == [TERM]
        assert know.tropes == [TROPE]
        assert know.cases == [CASE]

    def test_the_config_still_comes_from_the_heartbeat(self):
        assert knowledge_mod.fetch(FakeClient()).config == {'scan_interval': 30}

    def test_it_is_stamped_with_the_time_it_arrived(self):
        """What the freshness check reads. Without it a run cannot tell knowledge
        fetched a minute ago from knowledge fetched on Monday."""
        assert knowledge_mod.fetch(FakeClient()).age_seconds < 5


class TestTheOlderServer:
    def test_it_falls_back_rather_than_refusing_to_run(self):
        """A platform one release behind must not stop collection. The post
        being collected is deleted within hours; the deployment mismatch is
        fixed at somebody's convenience."""
        client = FakeClient(has_bundle=False)
        know = knowledge_mod.fetch(client)
        assert know.terms == [TERM]
        assert know.cases == [CASE]
        assert {'tasks', 'lexicon', 'tropes'} <= set(client.calls)

    def test_the_result_is_the_same_either_way(self):
        new = knowledge_mod.fetch(FakeClient())
        old = knowledge_mod.fetch(FakeClient(has_bundle=False))
        assert (new.terms, new.tropes, new.cases) == (old.terms, old.tropes, old.cases)
        assert new.versions == old.versions


class TestWhenItCannotSync:
    @pytest.mark.parametrize('broken', ['heartbeat', 'accounts'])
    def test_a_failure_aborts_the_run(self, broken):
        """Collecting against knowledge of unknown age produces findings that
        cannot be attributed to a release, and an audit chain with a hole in it
        is not an audit chain."""
        with pytest.raises(KnowledgeError):
            knowledge_mod.fetch(FakeClient(fail=broken))

    def test_the_message_says_why_rather_than_just_failing(self):
        with pytest.raises(KnowledgeError) as raised:
            knowledge_mod.fetch(FakeClient(fail='heartbeat'))
        assert 'unknown age' in str(raised.value)

    def test_the_fallback_failing_too_aborts_the_run(self):
        """Not swallowed into an empty bundle: no cases and no lexicon is a
        perfectly quiet run that collects nothing and reports success."""
        client = FakeClient(has_bundle=False, fail='lexicon')
        with pytest.raises(KnowledgeError):
            knowledge_mod.fetch(client)


class TestThePlatformsNameForTheBundle:
    """The platform archives each bundle it serves and hands back the key.

    Recorded on every finding, because it is what makes "produce the rules that
    judged this" answerable months later -- the agent's own digests answer the
    narrower question of whether the knowledge moved between two runs.

    Deliberately not compared against those digests. The two are computed
    differently on purpose: per collection here, over the whole bundle there. A
    mismatch would mean nothing, and a warning that fires on every sync teaches
    people to stop reading warnings.
    """

    def named(self, release_id):
        class Named(FakeClient):
            def bundle(self, languages=None):
                return {**super().bundle(languages), 'release_id': release_id}

        return knowledge_mod.fetch(Named())

    def test_it_is_kept(self):
        assert self.named('4@abc123').platform_release_id == '4@abc123'

    def test_it_travels_with_every_verdict(self):
        assert self.named('4@abc123').versions['release'] == '4@abc123'

    def test_the_agents_own_digests_are_still_there(self):
        """They answer a different question, and losing them would make a
        curator's edit invisible between two runs."""
        versions = self.named('4@abc123').versions
        assert versions['lexicon'] and versions['tropes'] and versions['cases']

    def test_a_platform_that_names_nothing_is_not_an_error(self, caplog):
        """The older three-call sync has no bundle to name. An empty key is
        honest; inventing one would make an unarchived release look retrievable."""
        with caplog.at_level('WARNING'):
            know = knowledge_mod.fetch(FakeClient(has_bundle=False))
        assert know.versions['release'] == ''
        assert not any('release' in r.message for r in caplog.records)
