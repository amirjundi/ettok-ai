# -*- coding: utf-8 -*-
"""The update check must ask about the repository this agent installs from.

Ettok is a deliberate divergence from the project it was forked out of -- the
rebrand, the hate-speech plugin, the collection safety rules -- so upstream is an
ancestor, not a source of truth. Pointing the check at the parent produced two
failures, both of which looked like features working:

  * `ettok update --check` compared against the parent and reported "764 commits
    behind" on a checkout that was current, so the dashboard lit an update badge
    that could never be cleared.

  * The compare call that turns two SHAs into a count and a changelog had the
    parent's name baked into the URL, so it asked GitHub about Ettok commits
    inside the Hermes repository, where they do not exist. That 404s, the error
    is swallowed, and the dashboard falls back to "an update is available" with
    no count and an empty "what's changed".
"""
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import banner
from hermes_cli.update_cmd_git import OFFICIAL_REPO_URL, OFFICIAL_REPO_URLS, _is_fork


@pytest.fixture(autouse=True)
def _clear_slug_cache():
    banner._origin_slug_cache = None
    yield
    banner._origin_slug_cache = None


def test_this_project_is_the_official_repo_not_a_fork_of_one():
    """`_is_fork` answering True is what makes the updater offer to add an
    `upstream` remote on an operator's machine and then sync against it."""
    assert not _is_fork("https://github.com/amirjundi/ettok-ai.git")
    assert not _is_fork("git@github.com:amirjundi/ettok-ai.git")
    assert _is_fork("https://github.com/NousResearch/hermes-agent.git")
    assert "ettok-ai" in OFFICIAL_REPO_URL
    assert all("ettok-ai" in url for url in OFFICIAL_REPO_URLS)


def test_the_slug_comes_from_the_origin_remote():
    with mock.patch.object(banner, "_resolve_repo_dir", return_value=Path(".")):
        with mock.patch.object(
            banner, "_git_stdout",
            return_value="https://github.com/amirjundi/ettok-ai.git",
        ):
            assert banner._origin_repo_slug() == "amirjundi/ettok-ai"


def test_an_ssh_origin_resolves_to_the_same_slug():
    with mock.patch.object(banner, "_resolve_repo_dir", return_value=Path(".")):
        with mock.patch.object(
            banner, "_git_stdout",
            return_value="git@github.com:amirjundi/ettok-ai.git",
        ):
            assert banner._origin_repo_slug() == "amirjundi/ettok-ai"


def test_a_missing_origin_falls_back_to_the_configured_repo():
    with mock.patch.object(banner, "_resolve_repo_dir", return_value=None):
        expected = banner._OFFICIAL_REPO_CANONICAL.removeprefix("github.com/")
        assert banner._origin_repo_slug() == expected


def test_the_compare_url_is_built_from_that_slug():
    """The bug this guards: the repository name was a literal in the URL, so the
    request asked the parent project about commits that only exist here."""
    requested = {}

    def capture(fn):
        # _github_compare closes over the URL it built, then hands the request to
        # _quiet. Reading the closure inspects the URL without a network call.
        requested["url"] = fn.__closure__[0].cell_contents
        return None

    with mock.patch.object(banner, "_origin_repo_slug", return_value="amirjundi/ettok-ai"):
        with mock.patch.object(banner, "_quiet", side_effect=capture):
            banner._github_compare("d" * 40, "e" * 40)

    assert "amirjundi/ettok-ai" in requested["url"]
    assert "hermes-agent" not in requested["url"]


def test_a_rate_limited_api_falls_back_to_ls_remote(tmp_path):
    """60 unauthenticated requests an hour, shared by everyone behind one address.

    When that is spent the API returns nothing, and the check used to give up --
    on a machine whose git remote would have answered immediately, unmetered.
    """
    tip, head = "a" * 40, "b" * 40
    calls = []

    def fake_git_run(args, **kwargs):
        calls.append(args)
        return mock.Mock(returncode=0, stdout=f"{tip}\trefs/heads/main\n")

    def fake_git_stdout(args, **kwargs):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/amirjundi/ettok-ai.git"
        if args[0] == "rev-parse":
            return head
        return None

    with mock.patch.object(banner, "_github_branch_tip", return_value=None), \
         mock.patch.object(banner, "_git_stdout", side_effect=fake_git_stdout), \
         mock.patch.object(banner, "_git_run", side_effect=fake_git_run), \
         mock.patch.object(banner, "_git_ok", return_value=False), \
         mock.patch.object(banner, "_github_compare_behind", return_value=3):
        behind = banner._check_via_local_git(tmp_path)

    assert behind == 3, "the ls-remote fallback did not produce a count"
    assert any("ls-remote" in args for args in calls), "ls-remote was never tried"


def test_matching_tips_report_up_to_date_without_any_api_call(tmp_path):
    """The common case costs nothing: the same SHA on both sides is up to date."""
    sha = "c" * 40

    def fake_git_stdout(args, **kwargs):
        if args[:2] == ["remote", "get-url"]:
            return "https://github.com/amirjundi/ettok-ai.git"
        if args[0] == "rev-parse":
            return sha
        return None

    with mock.patch.object(banner, "_github_branch_tip", return_value=sha), \
         mock.patch.object(banner, "_git_stdout", side_effect=fake_git_stdout), \
         mock.patch.object(banner, "_github_compare_behind") as compare:
        assert banner._check_via_local_git(tmp_path) == 0
        compare.assert_not_called()
