"""Entry point for the ``kiro-acp`` utility."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import pathlib
import shutil
import subprocess
import sys

from kiro_acp import __version__
from kiro_acp.acp import (
    ACPError,
    ACPProcessError,
    KiroAgent,
    MetadataUpdate,
    Session,
    StopReason,
    TextDelta,
    TurnComplete,
    TurnResult,
)
from kiro_acp.acp.kiro import describe_capabilities
from kiro_acp.acp.session import EffortNotSupported
from kiro_acp.cli.common import (
    EXIT_CANCELLED,
    EXIT_ERROR,
    EXIT_NOT_FOUND,
    EXIT_OK,
    EXIT_USAGE,
    add_agent_options,
    add_input_options,
    add_output_options,
    add_permission_options,
    build_agent,
    build_prompt_blocks,
    configure_logging,
    read_prompt_argument,
)
from kiro_acp.cli.render import JsonlRenderer, TextRenderer, print_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kiro-acp",
        description="Talk to the Kiro CLI through the Agent Client Protocol.",
    )
    parser.add_argument("--version", action="version", version=f"kiro-acp {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("prompt", help="send one prompt and print the reply")
    p.add_argument("prompt", nargs="?", help="prompt text ('-' reads stdin)")
    p.add_argument("--session", help="resume an existing session id instead of creating one")
    p.add_argument("--print-session-id", action="store_true", help="print the session id to stderr")
    add_agent_options(p)
    add_permission_options(p, default="ask")
    add_input_options(p)
    add_output_options(p)
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("chat", help="interactive multi-turn chat (one session)")
    p.add_argument("--session", help="resume an existing session id")
    add_agent_options(p)
    add_permission_options(p, default="ask")
    add_output_options(p)
    p.set_defaults(func=cmd_chat)

    p = sub.add_parser("models", help="list models advertised by Kiro")
    p.add_argument("--json", action="store_true")
    add_agent_options(p)
    p.set_defaults(func=cmd_models)

    p = sub.add_parser("agents", help="list Kiro agents (ACP modes)")
    p.add_argument("--json", action="store_true")
    add_agent_options(p)
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("sessions", help="list or delete stored sessions (v3 engine)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="span every workspace, not just --cwd")
    p.add_argument(
        "--delete",
        action="append",
        default=[],
        metavar="ID",
        help="delete this session id (repeatable)",
    )
    p.add_argument(
        "--prune",
        action="store_true",
        help="delete every listed session matching --title/--older-than",
    )
    p.add_argument(
        "--title",
        metavar="GLOB",
        help="with --prune: only sessions whose title matches this shell glob",
    )
    p.add_argument(
        "--older-than",
        type=float,
        metavar="HOURS",
        help="with --prune: only sessions not updated for this many hours",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="with --prune: show what would be deleted"
    )
    p.add_argument(
        "--yes", "-y", action="store_true", help="with --prune: skip the confirmation prompt"
    )
    add_agent_options(p)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("info", help="show agent capabilities from initialize")
    p.add_argument("--json", action="store_true")
    add_agent_options(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser(
        "codex-catalog",
        help="write a Codex model_catalog_json listing Kiro's models in direct tool mode",
    )
    p.add_argument("--output", "-o", help="file to write (default: stdout)")
    p.add_argument(
        "--codex-version",
        help="Codex version whose catalogue to use as reference (default: detected)",
    )
    p.add_argument("--context-window", type=int, help="context window to advertise for every model")
    add_agent_options(p)
    p.set_defaults(func=cmd_codex_catalog)

    p = sub.add_parser("doctor", help="check the kiro-cli installation and ACP handshake")
    add_agent_options(p)
    p.set_defaults(func=cmd_doctor)
    return parser


# ---------------------------------------------------------------------- commands


async def _open_session(
    agent: KiroAgent, args: argparse.Namespace, *, autopilot: bool | None
) -> Session:
    if getattr(args, "session", None):
        session = await agent.load_session(args.session)
        if args.model and args.model != session.model_id:
            await session.set_model(args.model)
        if args.agent and args.agent != session.mode_id:
            await session.set_mode(args.agent)
        if args.effort:
            with contextlib.suppress(EffortNotSupported):
                await session.set_effort(args.effort)
        return session
    return await agent.new_session(autopilot=autopilot)


def _autopilot(args: argparse.Namespace) -> bool | None:
    permissions = getattr(args, "permissions", None)
    if permissions is None:
        return None
    return permissions == "allow-all"


async def _run_turn(session: Session, blocks, args: argparse.Namespace) -> TurnResult:
    renderer = (
        JsonlRenderer()
        if args.output == "jsonl"
        else TextRenderer(
            show_tools=args.show_tools, show_thoughts=args.show_thoughts, quiet=args.quiet
        )
    )
    result = TurnResult()
    text: list[str] = []
    metadata: dict = {}
    async for event in session.prompt(blocks, timeout=args.timeout):
        if args.output != "json":
            renderer.handle(event)
        match event:
            case TextDelta(text=chunk):
                text.append(chunk)
            case MetadataUpdate(data=data):
                metadata.update(data)
            case TurnComplete(stop_reason=stop_reason, error=error):
                result.stop_reason = stop_reason
                result.error = error
    result.text = "".join(text)
    result.metadata = metadata
    return result


async def cmd_prompt(args: argparse.Namespace) -> int:
    text = read_prompt_argument(args.prompt)
    blocks = build_prompt_blocks(text, args)
    async with build_agent(args, interactive=args.output == "text") as agent:
        session = await _open_session(agent, args, autopilot=_autopilot(args))
        if args.print_session_id or args.debug:
            print(f"[session] {session.session_id}", file=sys.stderr)
        if args.output == "json":
            result = await session.collect(session.prompt(blocks, timeout=args.timeout))
            payload = result.to_dict()
            payload.update(
                {
                    "session_id": session.session_id,
                    "model": session.model_id,
                    "mode": session.mode_id,
                }
            )
            print_json(payload)
        else:
            result = await _run_turn(session, blocks, args)
        if session.effort_error and not args.quiet:
            print(f"[effort] {session.effort_error}", file=sys.stderr)
    if result.stop_reason == StopReason.ERROR:
        return EXIT_ERROR
    if result.stop_reason == StopReason.CANCELLED:
        return EXIT_CANCELLED
    return EXIT_OK


async def cmd_chat(args: argparse.Namespace) -> int:
    if args.output == "json":
        print("chat supports --output text or jsonl", file=sys.stderr)
        return EXIT_USAGE
    async with build_agent(args, interactive=True) as agent:
        session = await _open_session(agent, args, autopilot=_autopilot(args))
        print(
            f"kiro-acp chat — session {session.session_id} model={session.model_id} mode={session.mode_id}\n"
            "Commands: /model <id>, /effort <level>, /mode <id>, /session, /quit",
            file=sys.stderr,
        )
        while True:
            try:
                line = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                print(file=sys.stderr)
                break
            line = line.strip()
            if not line:
                continue
            if line in ("/quit", "/exit", "/q"):
                break
            if line == "/session":
                print(session.session_id, file=sys.stderr)
                continue
            if line.startswith(("/model ", "/effort ", "/mode ")):
                command, _, value = line.partition(" ")
                try:
                    if command == "/model":
                        await session.set_model(value.strip())
                    elif command == "/mode":
                        await session.set_mode(value.strip())
                    else:
                        await session.set_effort(value.strip())
                    print(f"[ok] {command[1:]} = {value.strip()}", file=sys.stderr)
                except (ACPError, ValueError) as error:
                    print(f"[error] {error}", file=sys.stderr)
                continue
            try:
                await _run_turn(session, build_prompt_blocks(line, args), args)
            except KeyboardInterrupt:
                await session.cancel()
    return EXIT_OK


async def cmd_models(args: argparse.Namespace) -> int:
    async with build_agent(args, interactive=False) as agent:
        info = await agent.discover()
    if args.json:
        print_json(
            {
                "current": info.current_model_id,
                "models": [
                    m.model_dump(by_alias=True, exclude_none=True) for m in info.available_models
                ],
            }
        )
        return EXIT_OK
    for model in info.available_models:
        marker = "*" if model.model_id == info.current_model_id else " "
        print(f"{marker} {model.model_id:<24} {model.description or ''}")
    return EXIT_OK


async def cmd_agents(args: argparse.Namespace) -> int:
    async with build_agent(args, interactive=False) as agent:
        info = await agent.discover()
    if args.json:
        print_json(
            {
                "current": info.current_mode_id,
                "agents": [
                    m.model_dump(exclude_none=True, exclude={"_meta"}) for m in info.available_modes
                ],
            }
        )
        return EXIT_OK
    for mode in info.available_modes:
        marker = "*" if mode.id == info.current_mode_id else " "
        print(f"{marker} {mode.id:<28} {mode.description or ''}")
    return EXIT_OK


async def cmd_sessions(args: argparse.Namespace) -> int:
    import fnmatch
    from datetime import UTC, datetime

    async with build_agent(args, interactive=False) as agent:
        if not agent.supports_session_list:
            print(
                f"The {args.engine} engine does not support session/list (use --engine v3).",
                file=sys.stderr,
            )
            return EXIT_ERROR
        if args.delete:
            failed = 0
            for session_id in args.delete:
                ok = await agent.delete_session(session_id)
                print(f"{'deleted' if ok else 'not supported'}: {session_id}", file=sys.stderr)
                failed += 0 if ok else 1
            return EXIT_OK if not failed else EXIT_ERROR
        sessions = await agent.list_sessions(cwd=None if args.all else args.cwd)
        if args.prune:
            return await _prune_sessions(agent, sessions, args, fnmatch, datetime, UTC)
    if args.json:
        print_json(sessions)
        return EXIT_OK
    for item in sessions:
        print(
            f"{item.get('sessionId', ''):<45} {item.get('updatedAt', ''):<26} {item.get('title', '')}"
        )
    return EXIT_OK


async def _prune_sessions(
    agent: KiroAgent, sessions: list, args: argparse.Namespace, fnmatch, datetime, UTC
) -> int:
    now = datetime.now(UTC)
    victims = []
    for item in sessions:
        title = str(item.get("title") or "")
        if args.title and not fnmatch.fnmatch(title, args.title):
            continue
        if args.older_than is not None:
            try:
                updated = datetime.fromisoformat(str(item.get("updatedAt")).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if (now - updated).total_seconds() / 3600 < args.older_than:
                continue
        victims.append(item)
    for item in victims:
        print(
            f"{item.get('sessionId', ''):<45} {item.get('updatedAt', ''):<26} {item.get('title', '')}"
        )
    print(f"{len(victims)} of {len(sessions)} sessions selected", file=sys.stderr)
    if args.dry_run or not victims:
        return EXIT_OK
    if not args.yes:
        answer = await asyncio.to_thread(input, f"Delete {len(victims)} sessions? [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            return EXIT_CANCELLED
    deleted = 0
    for item in victims:
        if await agent.delete_session(str(item.get("sessionId"))):
            deleted += 1
    print(f"deleted {deleted} sessions", file=sys.stderr)
    return EXIT_OK


async def cmd_info(args: argparse.Namespace) -> int:
    async with build_agent(args, interactive=False) as agent:
        details = describe_capabilities(agent.info)
        details["engine"] = args.engine
        details["command"] = agent.options.command()
    if args.json:
        print_json(details)
    else:
        for key, value in details.items():
            print(f"{key:<20} {json.dumps(value) if not isinstance(value, str) else value}")
    return EXIT_OK


async def cmd_codex_catalog(args: argparse.Namespace) -> int:
    from kiro_acp.cli.codex_catalog import (
        build_catalog,
        codex_version,
        fetch_reference_catalog,
        reference_entry,
    )
    from kiro_acp.gateway.protocols.models import context_window

    async with build_agent(args, interactive=False) as agent:
        info = await agent.discover()
    kiro_models = [
        {
            "id": m.model_id,
            "description": m.description,
            "context_length": context_window(m.description),
        }
        for m in info.available_models
    ]
    version = args.codex_version or codex_version()
    catalog, ref = await asyncio.to_thread(fetch_reference_catalog, version)
    reference = reference_entry(catalog)
    result = build_catalog(kiro_models, reference, context_window=args.context_window)
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        await asyncio.to_thread(pathlib.Path(args.output).write_text, text)
        print(
            f"wrote {len(result['models'])} models to {args.output} (reference: {reference.get('slug')} from {ref})\n"
            f'add to ~/.codex/config.toml:  model_catalog_json = "{args.output}"',
            file=sys.stderr,
        )
    else:
        print(text, end="")
    return EXIT_OK


async def cmd_doctor(args: argparse.Namespace) -> int:
    ok = True
    path = shutil.which(args.kiro)
    print(f"kiro-cli executable: {path or 'NOT FOUND'} ({args.kiro})")
    if not path:
        print("  install Kiro CLI and ensure it is on PATH, or set --kiro / KIRO_ACP_CLI")
        return EXIT_NOT_FOUND
    try:
        version = await asyncio.to_thread(
            subprocess.run, [path, "--version"], capture_output=True, text=True, timeout=30
        )
        print(f"kiro-cli version:    {(version.stdout or version.stderr).strip()}")
    except (OSError, subprocess.SubprocessError) as error:
        print(f"kiro-cli version:    failed ({error})")
        ok = False
    for engine in ("v3", "v2"):
        args.engine = engine
        try:
            async with build_agent(args, interactive=False) as agent:
                info = agent.info
                session_info = await agent.discover()
            print(
                f"engine {engine}:           ok — {info.agent_info.name} {info.agent_info.version}, "
                f"{len(session_info.model_ids)} models, default {session_info.current_model_id}"
            )
        except ACPError as error:
            ok = False
            print(f"engine {engine}:           FAILED — {str(error).splitlines()[0]}")
    print("workspace:           " + args.cwd)
    print("result:              " + ("healthy" if ok else "problems found"))
    return EXIT_OK if ok else EXIT_ERROR


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.debug)
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return EXIT_CANCELLED
    except ACPProcessError as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_NOT_FOUND if "not found" in str(error) else EXIT_ERROR
    except (ACPError, FileNotFoundError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return EXIT_USAGE if isinstance(error, ValueError) else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
