"""Credential handling: where .env is read from, and what a Config reveals."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import espn_mcp.config as config  # noqa: E402

VARS = ("ESPN_LEAGUE_ID", "ESPN_SEASON", "ESPN_TEAM_ID", "ESPN_S2", "SWID",
        "ESPN_POOL_TTL", "ESPN_STATE_DIR")


@pytest.fixture
def clean_env(monkeypatch):
    for var in VARS:
        monkeypatch.delenv(var, raising=False)


def test_env_file_is_read_from_the_project_root_only(tmp_path, monkeypatch, clean_env):
    # A .env in the working directory must not be able to swap the league.
    (tmp_path / ".env").write_text("ESPN_LEAGUE_ID=999\nESPN_S2=stolen\nSWID={x}\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "ENV_FILE", tmp_path / "nowhere" / ".env")
    with pytest.raises(RuntimeError, match="ESPN_LEAGUE_ID is not set"):
        config.load_config()


def test_env_file_at_the_pinned_path_is_read(tmp_path, monkeypatch, clean_env):
    env = tmp_path / "root" / ".env"
    env.parent.mkdir()
    env.write_text('ESPN_LEAGUE_ID=123\nESPN_TEAM_ID=4\nESPN_S2="s2value"\nSWID=abc\n')
    monkeypatch.setattr(config, "ENV_FILE", env)
    cfg = config.load_config()
    assert cfg.league_id == "123" and cfg.team_id == 4
    assert cfg.espn_s2 == "s2value"
    assert cfg.swid == "{abc}"  # braces restored


def test_real_environment_wins_over_the_file(tmp_path, monkeypatch, clean_env):
    env = tmp_path / ".env"
    env.write_text("ESPN_LEAGUE_ID=123\n")
    monkeypatch.setattr(config, "ENV_FILE", env)
    monkeypatch.setenv("ESPN_LEAGUE_ID", "456")
    assert config.load_config().league_id == "456"


def test_repr_and_str_never_contain_the_cookies():
    cfg = config.Config(league_id="1", season=2026, team_id=None, espn_s2="SECRET-S2",
                        swid="{SECRET-SWID}", pool_ttl=900, state_dir=None)
    for text in (repr(cfg), str(cfg), f"{cfg}"):
        assert "SECRET" not in text
        assert "***" in text
    assert cfg.secrets == ("SECRET-S2", "{SECRET-SWID}")
    empty = config.Config(league_id="1", season=2026, team_id=None, espn_s2=None,
                          swid=None, pool_ttl=900, state_dir=None)
    assert "espn_s2=None" in repr(empty) and empty.secrets == ()
