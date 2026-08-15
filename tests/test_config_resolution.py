"""Test that config resolution works in installed/external environments.

This addresses the GitHub Actions failure where config paths were resolved
relative to Python's installation directory instead of the repository.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from hsc_queue_monitor.models import ConfigError


def test_monitor_once_does_not_load_selectors_yaml(tmp_path, monkeypatch):
    """monitor-once should not fail when selectors.yaml is missing."""
    from hsc_queue_monitor.config import AppConfig

    # Create a minimal config directory with only what monitor-once needs
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # Create only the files monitor-once actually uses
    (config_dir / "app.yaml").write_text(
        "mongodb:\n  database: test\n  session_collection: session\n"
        "api:\n  monitor_interval_seconds: 300\n"
        "  connect_timeout_seconds: 5\n"
        "  read_timeout_seconds: 60\n"
        "  slot_request_interval_seconds: 3\n"
        "  retry:\n    max_attempts: 3\n    initial_backoff_seconds: 2\n"
        "    max_backoff_seconds: 30\n"
    )
    (config_dir / "service_centers.yaml").write_text(
        "service_centers:\n  - id: '3242'\n    name: 'Test Center'\n    enabled: true\n"
    )

    # Do NOT create selectors.yaml or flow.yaml

    # This should not fail — monitor-once doesn't need them
    config = AppConfig.load(config_dir=config_dir)
    assert config is not None

    # But accessing them should fail (they weren't loaded)
    with pytest.raises(ConfigError, match="Missing configuration file"):
        _ = config.selectors

    with pytest.raises(ConfigError, match="Missing configuration file"):
        _ = config.flow


def test_config_resolution_from_repository_root(tmp_path, monkeypatch):
    """Test that config is found when cwd is the repository root."""
    from hsc_queue_monitor.config import _find_config_dir

    # Mock being in the repository root
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))

    # Should find config in current directory
    found = _find_config_dir()
    assert found.name == "config"


def test_config_resolution_from_current_directory(tmp_path, monkeypatch):
    """Test that config is found from cwd when repository root not available."""
    from hsc_queue_monitor.config import _find_config_dir

    # Create config in current directory
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    monkeypatch.chdir(tmp_path)

    # Should find config in cwd
    found = _find_config_dir()
    assert found.is_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="Package building differs on Windows")
def test_monitor_once_with_headless_config_only():
    """Integration test: monitor-once should work with only headless config."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_dir = Path(tmpdir) / "config"
        config_dir.mkdir()

        # Write minimal headless-only config
        (config_dir / "app.yaml").write_text(
            "mongodb:\n  database: hsc\n  session_collection: hsc-api-session\n"
            "api:\n  monitor_interval_seconds: 300\n"
            "  connect_timeout_seconds: 5\n"
            "  read_timeout_seconds: 60\n"
            "  slot_request_interval_seconds: 3\n"
            "  retry:\n    max_attempts: 3\n    initial_backoff_seconds: 2\n"
            "    max_backoff_seconds: 30\n"
        )
        (config_dir / "service_centers.yaml").write_text(
            "service_centers:\n  - id: '3242'\n    name: 'ТСЦ 3242'\n    enabled: true\n"
        )

        # Import and test config loading
        from hsc_queue_monitor.config import AppConfig

        config = AppConfig.load(config_dir=config_dir)
        assert config.app is not None
        assert len(config.service_centers) > 0

        # Access _selectors/_flow fields directly to check if they're lazy loaded
        sel = object.__getattribute__(config, "_selectors")
        flow = object.__getattribute__(config, "_flow")
        assert sel is None, "Selectors should not be loaded yet"
        assert flow is None, "Flow should not be loaded yet"
