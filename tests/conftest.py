from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

FAKE_AGENT = Path(__file__).parent / "fake_agent" / "agent.py"


def fake_agent_command(engine: str = "v2") -> list[str]:
    return [sys.executable, str(FAKE_AGENT)]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "notes.txt").write_text("hello from notes\n")
    return tmp_path


@pytest.fixture(params=["v2", "v3"])
def engine(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("FAKE_ACP_ENGINE", request.param)
    return request.param


def integration_enabled() -> bool:
    return os.environ.get("KIRO_INTEGRATION") == "1"
