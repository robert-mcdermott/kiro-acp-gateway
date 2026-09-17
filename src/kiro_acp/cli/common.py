"""Shared argument parsing and agent construction for CLI subcommands."""

from __future__ import annotations

import argparse
import base64
import mimetypes
import os
import sys
from pathlib import Path

from kiro_acp import __version__
from kiro_acp.acp import (
    ClientHandlers,
    KiroAgent,
    KiroLaunchOptions,
    LocalFileSystem,
    LocalTerminals,
    PermissionPolicy,
    PermissionRequest,
    PermissionRule,
)
from kiro_acp.acp.handlers import POLICY_NAMES
from kiro_acp.acp.kiro import DEFAULT_ENGINE, ENGINES
from kiro_acp.acp.session import EFFORT_LEVELS, image_block, resource_link_block, text_block
from kiro_acp.acp.types import JSON

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CANCELLED = 3
EXIT_NOT_FOUND = 4


def add_agent_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("kiro")
    group.add_argument(
        "--kiro",
        default=os.environ.get("KIRO_ACP_CLI", "kiro-cli"),
        help="kiro-cli executable (env KIRO_ACP_CLI)",
    )
    group.add_argument(
        "--engine",
        choices=ENGINES,
        default=os.environ.get("KIRO_ACP_ENGINE", DEFAULT_ENGINE),
        help=f"Kiro agent engine (env KIRO_ACP_ENGINE, default {DEFAULT_ENGINE})",
    )
    group.add_argument(
        "--cwd",
        default=os.environ.get("KIRO_ACP_WORKSPACE") or os.getcwd(),
        help="workspace directory (env KIRO_ACP_WORKSPACE)",
    )
    group.add_argument(
        "--model",
        default=os.environ.get("KIRO_ACP_MODEL"),
        help="model id, e.g. claude-sonnet-4.6 (env KIRO_ACP_MODEL)",
    )
    group.add_argument(
        "--effort",
        choices=EFFORT_LEVELS,
        default=os.environ.get("KIRO_ACP_EFFORT"),
        help="reasoning effort (env KIRO_ACP_EFFORT)",
    )
    group.add_argument(
        "--agent",
        "--mode",
        dest="agent",
        default=os.environ.get("KIRO_ACP_AGENT"),
        help="Kiro agent / ACP mode id (env KIRO_ACP_AGENT)",
    )
    group.add_argument(
        "--timeout",
        type=float,
        default=float(os.environ.get("KIRO_ACP_TIMEOUT", "900")),
        help="turn timeout in seconds",
    )
    group.add_argument("--debug", action="store_true", help="log raw ACP traffic and agent stderr")


def add_permission_options(parser: argparse.ArgumentParser, *, default: str) -> None:
    group = parser.add_argument_group("permissions")
    group.add_argument(
        "--permissions",
        choices=POLICY_NAMES,
        default=os.environ.get("KIRO_ACP_PERMISSIONS", default),
        help=f"how to answer tool permission requests (env KIRO_ACP_PERMISSIONS, default {default})",
    )
    group.add_argument(
        "--allow",
        action="append",
        default=[],
        metavar="RULE",
        help="allow rule, e.g. 'kind=read,search' or 'tool=shell;title=Running: ls*' (repeatable)",
    )
    group.add_argument(
        "--deny",
        action="append",
        default=[],
        metavar="RULE",
        help="deny rule (repeatable, evaluated in order with --allow)",
    )
    group.add_argument(
        "--fs",
        action="store_true",
        help="serve fs/read_text_file and fs/write_text_file from the workspace",
    )
    group.add_argument(
        "--terminal", action="store_true", help="serve terminal/* requests with local subprocesses"
    )


def add_input_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("input")
    group.add_argument(
        "--image", action="append", default=[], metavar="PATH", help="attach an image (repeatable)"
    )
    group.add_argument(
        "--file",
        action="append",
        default=[],
        metavar="PATH",
        help="attach a file as a resource link (repeatable)",
    )


def add_output_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("output")
    group.add_argument(
        "--output", "-o", choices=("text", "json", "jsonl"), default="text", help="output format"
    )
    group.add_argument(
        "--show-tools", action="store_true", help="print tool activity to stderr in text mode"
    )
    group.add_argument(
        "--show-thoughts", action="store_true", help="print agent thoughts to stderr in text mode"
    )
    group.add_argument("--quiet", "-q", action="store_true", help="suppress status lines on stderr")


def build_rules(args: argparse.Namespace) -> list[PermissionRule]:
    rules: list[PermissionRule] = []
    for text in args.allow:
        rules.append(PermissionRule.parse(f"allow:{text}"))
    for text in args.deny:
        rules.append(PermissionRule.parse(f"deny:{text}"))
    return rules


async def ask_on_tty(request: PermissionRequest) -> JSON:
    """Interactive permission prompt on the controlling terminal."""
    import asyncio
    import json

    from kiro_acp.acp.types import cancelled, selected

    err = sys.stderr
    print(f"\nPermission request: {request.title} [{request.kind.value}]", file=err)
    if request.raw_input is not None:
        print(json.dumps(request.raw_input, indent=2)[:2000], file=err)
    if not request.options:
        print("No options offered; denying.", file=err)
        return cancelled()
    for index, option in enumerate(request.options, start=1):
        print(f"  {index}. {option.name} [{option.kind.value}]", file=err)
    print("  0. Cancel", file=err)

    def read() -> str:
        print("Select: ", end="", file=err, flush=True)
        try:
            with open("/dev/tty") as tty:  # noqa: PTH123 - controlling terminal, not a file
                return tty.readline().strip()
        except OSError:
            return sys.stdin.readline().strip()

    answer = await asyncio.to_thread(read)
    try:
        choice = int(answer or "0")
    except ValueError:
        choice = 0
    if 1 <= choice <= len(request.options):
        return selected(request.options[choice - 1].option_id)
    return cancelled()


def build_handlers(args: argparse.Namespace, *, interactive: bool) -> ClientHandlers:
    mode = args.permissions
    callback = None
    if mode == "ask":
        if not interactive or not (sys.stdin.isatty() or os.path.exists("/dev/tty")):
            mode = "deny"
        else:
            callback = ask_on_tty
    policy = PermissionPolicy(mode, rules=build_rules(args), callback=callback)
    return ClientHandlers(
        permissions=policy,
        filesystem=LocalFileSystem(args.cwd) if args.fs else None,
        terminals=LocalTerminals(args.cwd) if args.terminal else None,
    )


def build_agent(
    args: argparse.Namespace, *, interactive: bool, model: str | None = None
) -> KiroAgent:
    options = KiroLaunchOptions(
        executable=args.kiro,
        engine=args.engine,
        model=model if model is not None else args.model,
        effort=args.effort,
        agent=args.agent,
        trust_all_tools=args.permissions == "allow-all" if hasattr(args, "permissions") else False,
        verbose=1 if args.debug else 0,
    )
    return KiroAgent(
        options,
        cwd=args.cwd,
        handlers=build_handlers(args, interactive=interactive)
        if hasattr(args, "permissions")
        else None,
        client_name="kiro-acp",
        client_version=__version__,
    )


def build_prompt_blocks(text: str, args: argparse.Namespace) -> list[JSON]:
    blocks: list[JSON] = []
    for path in getattr(args, "file", []) or []:
        file_path = Path(path).resolve()
        if not file_path.exists():
            raise FileNotFoundError(f"attachment not found: {path}")
        mime, _ = mimetypes.guess_type(str(file_path))
        blocks.append(resource_link_block(file_path.as_uri(), file_path.name, mime_type=mime))
    for path in getattr(args, "image", []) or []:
        image_path = Path(path).resolve()
        if not image_path.exists():
            raise FileNotFoundError(f"image not found: {path}")
        mime, _ = mimetypes.guess_type(str(image_path))
        data = base64.b64encode(image_path.read_bytes()).decode("ascii")
        blocks.append(
            image_block(data, mime or "application/octet-stream", uri=image_path.as_uri())
        )
    if text:
        blocks.append(text_block(text))
    if not blocks:
        raise ValueError("empty prompt")
    return blocks


def read_prompt_argument(value: str | None) -> str:
    """Return the prompt text; ``-`` or a missing value reads stdin."""
    if value is None or value == "-":
        if sys.stdin.isatty() and value is None:
            raise ValueError("no prompt given (pass text, or '-' to read stdin)")
        return sys.stdin.read().strip()
    return value


def configure_logging(debug: bool) -> None:
    import logging

    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    if debug:
        logging.getLogger("kiro_acp.acp.wire").setLevel(logging.DEBUG)
    else:
        logging.getLogger("kiro_acp.acp.client").setLevel(logging.WARNING)
