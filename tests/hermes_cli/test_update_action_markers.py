"""update.log is written by two different callers, and they disagree on wording.

A dashboard-triggered update is stamped with its action label
(``=== hermes-update started ...``, see ``web_server_gateway._spawn``); a CLI run
is stamped with the product name (``=== ettok update started ...``, see
``main_dashboard``). The reader knew only the second spelling, so on the
dashboard path -- the only path that needs this function at all, since that is
where the update restarts the process holding the in-memory result -- the "last
start" index stayed at -1 and any older completion counted as newer than it.

The consequence is the one the docstring promises cannot happen: a failed update
reported as the previous run's success.
"""

from __future__ import annotations

from hermes_cli.web_routers.actions import _durable_completed_update_action_id

OLD = "a" * 32
NEW = "b" * 32


def _dashboard(ts: str) -> str:
    return f"=== hermes-update started {ts} ==="


def _cli(ts: str) -> str:
    return f"=== ettok update started {ts} ==="


def _completed(action_id: str) -> str:
    return f"=== hermes-update completed {action_id} ==="


def test_a_finished_update_is_reported():
    assert _durable_completed_update_action_id(
        [_dashboard("2026-09-16 10:00:00"), _completed(OLD)]) == OLD


def test_a_newer_failed_run_is_not_masked_by_an_older_success():
    """The whole point of the function. The dashboard spelling used to skip it."""
    lines = [
        _dashboard("2026-09-16 10:00:00"), _completed(OLD),
        _dashboard("2026-09-16 11:12:00"), "Update failed.",
    ]
    assert _durable_completed_update_action_id(lines) is None


def test_a_newer_success_supersedes_an_older_one():
    lines = [
        _dashboard("2026-09-16 10:00:00"), _completed(OLD),
        _dashboard("2026-09-16 11:12:00"), _completed(NEW),
    ]
    assert _durable_completed_update_action_id(lines) == NEW


def test_the_cli_spelling_still_works():
    assert _durable_completed_update_action_id(
        [_cli("2026-09-16T11:12:00"), "Update failed."]) is None
    assert _durable_completed_update_action_id(
        [_cli("2026-09-16T10:00:00"), _completed(OLD)]) == OLD


def test_mixed_spellings_in_one_log_are_ordered_correctly():
    """Both writers append to the same file, so a real log interleaves them."""
    lines = [
        _cli("2026-09-16T09:00:00"), _completed(OLD),
        _dashboard("2026-09-16 11:12:00"), "Update failed.",
    ]
    assert _durable_completed_update_action_id(lines) is None


def test_no_markers_at_all():
    assert _durable_completed_update_action_id([]) is None
    assert _durable_completed_update_action_id(["just some output"]) is None
