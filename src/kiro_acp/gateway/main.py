"""Entry point for ``kiro-gateway``."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from kiro_acp import __version__
from kiro_acp.gateway.app import create_app
from kiro_acp.gateway.config import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiro-gateway",
        description="OpenAI/Anthropic-compatible HTTP gateway for the Kiro CLI (ACP).",
    )
    parser.add_argument("--version", action="version", version=f"kiro-gateway {__version__}")
    parser.add_argument("--host", help="bind address (env KIRO_GATEWAY_HOST)")
    parser.add_argument("--port", type=int, help="port (env KIRO_GATEWAY_PORT)")
    parser.add_argument(
        "--workspace", help="directory Kiro operates in (env KIRO_GATEWAY_WORKSPACE)"
    )
    parser.add_argument(
        "--engine", choices=("v3", "v2", "v1"), help="Kiro agent engine (env KIRO_GATEWAY_ENGINE)"
    )
    parser.add_argument(
        "--permissions",
        choices=("deny", "allow-once", "allow-always", "allow-all"),
        help="tool permission policy (env KIRO_GATEWAY_PERMISSIONS)",
    )
    parser.add_argument(
        "--api-key", help="require this bearer token / x-api-key (env KIRO_GATEWAY_API_KEY)"
    )
    parser.add_argument(
        "--default-model",
        help="model used when a request's model is unknown (env KIRO_GATEWAY_DEFAULT_MODEL)",
    )
    parser.add_argument(
        "--session-mode",
        choices=("affinity", "stateless"),
        help="session reuse strategy (env KIRO_GATEWAY_SESSION_MODE)",
    )
    parser.add_argument(
        "--max-concurrency",
        type=int,
        help="max simultaneous Kiro turns (env KIRO_GATEWAY_MAX_CONCURRENCY)",
    )
    parser.add_argument("--log-level", help="uvicorn log level (env KIRO_GATEWAY_LOG_LEVEL)")
    parser.add_argument("--debug-acp", action="store_true", help="log raw ACP traffic")
    parser.add_argument(
        "--print-config", action="store_true", help="print the effective configuration and exit"
    )
    parser.add_argument(
        "--print-service",
        choices=("launchd", "systemd"),
        help="print a launchd plist / systemd unit that runs this gateway with an env file, and exit",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="env file the printed service loads (default .env in the project directory)",
    )
    return parser


def service_definition(kind: str, *, env_file: str, project_dir: str) -> str:
    """A launchd plist or systemd unit that runs ``uv run kiro-gateway`` from ``project_dir``."""
    import shutil

    uv = shutil.which("uv") or "uv"
    env_path = os.path.abspath(os.path.join(project_dir, env_file))
    if kind == "systemd":
        return f"""[Unit]
Description=Kiro ACP Gateway (OpenAI/Anthropic-compatible API for Kiro CLI)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={project_dir}
EnvironmentFile=-{env_path}
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart={uv} run kiro-gateway
Restart=on-failure
RestartSec=5
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=default.target
"""
    # launchd reads no env file, so the wrapper shell sources it.
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>dev.kiro.acp-gateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/sh</string>
    <string>-c</string>
    <string>[ -f "{env_path}" ] &amp;&amp; set -a &amp;&amp; . "{env_path}" &amp;&amp; set +a; exec "{uv}" run kiro-gateway</string>
  </array>
  <key>WorkingDirectory</key><string>{project_dir}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>{os.path.expanduser("~/.local/bin")}:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>{os.path.expanduser("~/Library/Logs/kiro-gateway.log")}</string>
  <key>StandardErrorPath</key><string>{os.path.expanduser("~/Library/Logs/kiro-gateway.log")}</string>
</dict>
</plist>
"""


def settings_from_args(args: argparse.Namespace) -> Settings:
    overrides = {
        key: value
        for key, value in {
            "host": args.host,
            "port": args.port,
            "workspace": args.workspace,
            "engine": args.engine,
            "permissions": args.permissions,
            "api_key": args.api_key,
            "default_model": args.default_model,
            "session_mode": args.session_mode,
            "max_concurrency": args.max_concurrency,
            "log_level": args.log_level,
            "debug_acp": True if args.debug_acp else None,
        }.items()
        if value is not None
    }
    return Settings(**overrides)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.print_service:
        print(
            service_definition(args.print_service, env_file=args.env_file, project_dir=os.getcwd()),
            end="",
        )
        return 0
    try:
        settings = settings_from_args(args)
    except Exception as error:  # pydantic validation
        print(f"configuration error: {error}", file=sys.stderr)
        return 2
    if args.print_config:
        data = settings.model_dump()
        if data.get("api_key"):
            data["api_key"] = "***"
        data["api_keys"] = ["***"] * len(data.get("api_keys", []))
        for key, value in data.items():
            print(f"{key:<28} {value}")
        return 0
    if not os.path.isdir(settings.workspace):
        print(f"workspace is not a directory: {settings.workspace}", file=sys.stderr)
        return 2
    for legacy in ("KIRO_API_KEY", "KIRO_WORKSPACE", "KIRO_PERMISSIONS", "KIRO_PORT"):
        if legacy in os.environ:
            print(
                f"warning: {legacy} is set. Gateway settings use the KIRO_GATEWAY_ prefix "
                f"(e.g. KIRO_GATEWAY_{legacy[5:]}); KIRO_* variables are passed through to kiro-cli, "
                "and KIRO_API_KEY in particular breaks Kiro's own authentication.",
                file=sys.stderr,
            )
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if settings.debug_acp:
        logging.getLogger("kiro_acp.acp.wire").setLevel(logging.DEBUG)
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())
    return 0


if __name__ == "__main__":
    sys.exit(main())
