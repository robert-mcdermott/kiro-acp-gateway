from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import FAKE_AGENT


def run_cli(
    *argv: str, engine: str = "v2", cwd: Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    # The CLI launches `<kiro> acp --agent-engine ...`; point --kiro at a shim that ignores those args.
    shim = cwd / "kiro-shim.sh" if cwd else None
    assert shim is not None
    shim.write_text(
        f'#!/bin/sh\nexport FAKE_ACP_ENGINE={engine}\nexec "{sys.executable}" "{FAKE_AGENT}"\n'
    )
    shim.chmod(0o755)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "kiro_acp.cli.main",
            *argv,
            "--kiro",
            str(shim),
            "--cwd",
            str(cwd),
            "--engine",
            engine,
        ],
        capture_output=True,
        text=True,
        input=stdin,
        timeout=60,
    )


@pytest.mark.parametrize("engine", ["v2", "v3"])
def test_prompt_text_output(workspace: Path, engine: str) -> None:
    proc = run_cli(
        "prompt", "echo: hi there", "--permissions", "deny", engine=engine, cwd=workspace
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hi there\n"


def test_prompt_from_stdin_json(workspace: Path) -> None:
    proc = run_cli(
        "prompt",
        "-",
        "--output",
        "json",
        "--permissions",
        "allow-once",
        cwd=workspace,
        stdin="tool",
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["text"] == "done"
    assert payload["stop_reason"] == "end_turn"
    assert payload["tool_calls"][0]["title"] == "Running: ls"
    assert payload["permissions"][0]["granted"] is True
    assert payload["session_id"] == "fake-1"


def test_prompt_jsonl_events(workspace: Path) -> None:
    proc = run_cli("prompt", "thought", "--output", "jsonl", "--permissions", "deny", cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines()]
    kinds = [e["type"] for e in events]
    assert "thought" in kinds and "text" in kinds and kinds[-1] == "turn_complete"


def test_prompt_error_exit_code(workspace: Path) -> None:
    proc = run_cli("prompt", "error", "--permissions", "deny", cwd=workspace)
    assert proc.returncode == 1
    assert "Encountered an error" in proc.stderr


def test_prompt_model_and_show_tools(workspace: Path) -> None:
    proc = run_cli(
        "prompt",
        "tool",
        "--model",
        "claude-sonnet-4.6",
        "--permissions",
        "allow-once",
        "--show-tools",
        cwd=workspace,
    )
    assert proc.returncode == 0, proc.stderr
    assert "[tool] Running: ls" in proc.stderr
    assert "[permission] Running: ls: allowed" in proc.stderr


def test_models_and_agents(workspace: Path) -> None:
    proc = run_cli("models", "--json", cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    assert [m["modelId"] for m in json.loads(proc.stdout)["models"]] == [
        "claude-opus-4.8",
        "claude-sonnet-4.6",
        "gpt-5.6-terra",
    ]
    proc = run_cli("agents", cwd=workspace)
    assert "kiro_planner" in proc.stdout


def test_info_and_sessions(workspace: Path) -> None:
    proc = run_cli("info", "--json", engine="v3", cwd=workspace)
    assert json.loads(proc.stdout)["sessionCapabilities"] == ["list"]
    proc = run_cli("sessions", "--json", engine="v3", cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    proc = run_cli("sessions", engine="v2", cwd=workspace)
    assert proc.returncode == 1


def test_sessions_delete_and_prune(workspace: Path) -> None:
    run_cli("prompt", "echo: a", "--permissions", "deny", engine="v3", cwd=workspace)
    proc = run_cli(
        "sessions", "--prune", "--title", "Session *", "--dry-run", engine="v3", cwd=workspace
    )
    assert proc.returncode == 0, proc.stderr
    proc = run_cli("sessions", "--delete", "fake-1", engine="v3", cwd=workspace)
    assert proc.returncode == 0 and "deleted: fake-1" in proc.stderr
    proc = run_cli("sessions", "--prune", "--older-than", "1", "--yes", engine="v3", cwd=workspace)
    assert proc.returncode == 0, proc.stderr


def test_missing_executable(tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kiro_acp.cli.main",
            "prompt",
            "hi",
            "--kiro",
            "/nonexistent/kiro",
            "--permissions",
            "deny",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 4
    assert "not found" in proc.stderr


def test_gateway_print_service_units(tmp_path, monkeypatch, capsys) -> None:
    from kiro_acp.gateway.main import main as gateway_main

    monkeypatch.chdir(tmp_path)
    assert gateway_main(["--print-service", "systemd", "--env-file", "gw.env"]) == 0
    unit = capsys.readouterr().out
    assert "[Service]" in unit and str(tmp_path / "gw.env") in unit and "kiro-gateway" in unit
    assert gateway_main(["--print-service", "launchd"]) == 0
    plist = capsys.readouterr().out
    assert "<plist" in plist and "dev.kiro.acp-gateway" in plist and str(tmp_path) in plist


def test_gateway_env_file_resolution(tmp_path, monkeypatch) -> None:
    from kiro_acp.gateway import main as gateway_main_module
    from kiro_acp.gateway.main import build_parser, resolve_env_file, settings_from_args

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KIRO_GATEWAY_PORT", raising=False)
    monkeypatch.setattr(gateway_main_module, "DEFAULT_ENV_FILES", (".env", str(tmp_path / "none")))
    assert resolve_env_file(None) is None
    (tmp_path / "custom.env").write_text("KIRO_GATEWAY_PORT=9555\n")
    settings = settings_from_args(build_parser().parse_args(["--env-file", "custom.env"]))
    assert settings.port == 9555
    (tmp_path / ".env").write_text("KIRO_GATEWAY_PORT=9666\n")
    assert resolve_env_file(None) == ".env"
    assert settings_from_args(build_parser().parse_args([])).port == 9666
    with pytest.raises(FileNotFoundError):
        resolve_env_file("missing.env")
