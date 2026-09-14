"""Tests for Ettok-managed Camofox state helpers."""

from unittest.mock import patch


def _load_module():
    from tools import browser_camofox_state as state
    return state


class TestCamofoxStatePaths:
    def test_paths_are_profile_scoped(self, tmp_path):
        state = _load_module()
        with patch.object(state, "get_hermes_home", return_value=tmp_path):
            assert state.get_camofox_state_dir() == tmp_path / "browser_auth" / "camofox"


class TestCamofoxIdentity:
    def test_identity_is_deterministic(self, tmp_path):
        state = _load_module()
        with patch.object(state, "get_hermes_home", return_value=tmp_path):
            first = state.get_camofox_identity("task-1")
            second = state.get_camofox_identity("task-1")
            assert first == second


    def test_default_task_id(self, tmp_path):
        state = _load_module()
        with patch.object(state, "get_hermes_home", return_value=tmp_path):
            identity = state.get_camofox_identity()
            assert "user_id" in identity
            assert "session_key" in identity
            assert identity["user_id"].startswith("hermes_")
            assert identity["session_key"].startswith("task_")


class TestCamofoxConfigDefaults:
    def test_default_config_includes_camofox_controls(self):
        from hermes_cli.config import DEFAULT_CONFIG

        browser_cfg = DEFAULT_CONFIG["browser"]
        # True in this fork: the agent signs in once and reads with that account for
        # weeks, so a random profile per task would collect logged out. See
        # hermes_cli/config_defaults.py for why it is a default and not a wizard write.
        assert browser_cfg["camofox"]["managed_persistence"] is True
        assert browser_cfg["camofox"]["user_id"] == ""
        assert browser_cfg["camofox"]["session_key"] == ""
        assert browser_cfg["camofox"]["adopt_existing_tab"] is False
