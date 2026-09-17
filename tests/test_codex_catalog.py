from __future__ import annotations

from kiro_acp.cli.codex_catalog import build_catalog, reference_entry


def test_build_catalog_clones_reference_in_direct_mode() -> None:
    catalog = {
        "models": [
            {
                "slug": "gpt-5.4",
                "display_name": "x",
                "base_instructions": "You are Codex",
                "tool_mode": "direct",
                "use_responses_lite": False,
                "shell_type": "unified_exec",
                "priority": 9,
            },
            {
                "slug": "gpt-5.6-luna",
                "display_name": "luna",
                "base_instructions": "You are Codex (luna)",
                "tool_mode": "code_mode_only",
                "use_responses_lite": True,
                "prefer_websockets": True,
                "shell_type": "unified_exec",
                "priority": 1,
                "context_window": 272000,
            },
        ]
    }
    ref = reference_entry(catalog)
    assert ref["slug"] == "gpt-5.6-luna"
    out = build_catalog(
        [
            {
                "id": "claude-sonnet-4.6",
                "description": "Claude Sonnet 4.6 model with 1M context window",
                "context_length": 1_000_000,
            },
            {"id": "gpt-5.6-luna", "description": None, "context_length": 200_000},
        ],
        ref,
    )
    assert [m["slug"] for m in out["models"]] == ["claude-sonnet-4.6", "gpt-5.6-luna"]
    for entry in out["models"]:
        assert entry["tool_mode"] == "direct" and entry["use_responses_lite"] is False
        assert entry["base_instructions"] == "You are Codex (luna)"
        assert entry["prefer_websockets"] is False
    assert out["models"][0]["context_window"] == 1_000_000
    assert out["models"][0]["display_name"] == "claude-sonnet-4.6 (Kiro)"
