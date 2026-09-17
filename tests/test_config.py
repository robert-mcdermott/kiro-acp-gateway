from __future__ import annotations

import pytest

from kiro_acp.gateway.config import Settings


def test_list_settings_parse_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("KIRO_GATEWAY_ALLOWED_WORKSPACES", f"{tmp_path}/a*, {tmp_path}/b/**")
    monkeypatch.setenv("KIRO_GATEWAY_PERMISSION_RULES", "allow:kind=read;deny:Bash(rm *)")
    monkeypatch.setenv("KIRO_GATEWAY_API_KEYS", '["k1", "k2"]')
    monkeypatch.setenv("KIRO_GATEWAY_MODEL_ALIASES", "gpt-4*=gpt-5.6-terra,o3=claude-opus-4.6")
    settings = Settings(_env_file=None, workspace=str(tmp_path))
    assert settings.allowed_workspaces == [f"{tmp_path}/a*", f"{tmp_path}/b/**"]
    assert settings.permission_rules == ["allow:kind=read", "deny:Bash(rm *)"]
    assert settings.api_keys == ["k1", "k2"]
    assert settings.model_aliases == {"gpt-4*": "gpt-5.6-terra", "o3": "claude-opus-4.6"}
    (tmp_path / "alpha").mkdir()
    (tmp_path / "b" / "deep" / "er").mkdir(parents=True)
    assert settings.workspace_allowed(str(tmp_path / "alpha"))
    assert settings.workspace_allowed(str(tmp_path / "b" / "deep" / "er"))
    (tmp_path / "c").mkdir()
    assert not settings.workspace_allowed(str(tmp_path / "c"))  # not matched by any pattern
    assert settings.workspace_allowed(str(tmp_path))  # the default workspace is always allowed
