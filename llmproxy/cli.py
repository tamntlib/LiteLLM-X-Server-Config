"""Dynamic CLI entrypoint for generic and component-owned commands."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from llmproxy import __version__
from llmproxy.core.command import CommandError, CommandSpec, discover_commands
from llmproxy.core.context import AppContext, ComponentContext
from llmproxy.core.env import EnvError
from llmproxy.core.resources import validate_resource_root
from llmproxy.deployment.compose import ROOT, DeploymentError
from llmproxy.deployment.discovery import ComponentError, discover_components
from llmproxy.deployment.preset import PresetError


def create_parser(
    root: Path = ROOT,
    argv: list[str] | None = None,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llmproxy",
        description="Component-first Docker Swarm and LiteLLM operations CLI",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    root_subparsers = parser.add_subparsers(dest="command", help="Commands")
    subparsers_by_prefix: dict[tuple[str, ...], argparse._SubParsersAction] = {
        (): root_subparsers
    }

    commands = discover_commands(root)
    active_lazy_path: tuple[str, ...] | None = None
    if argv is not None:
        matches = [
            command.path
            for command in commands
            if command.lazy and tuple(argv[: len(command.path)]) == command.path
        ]
        if matches:
            active_lazy_path = max(matches, key=len)

    for command in commands:
        prefix: tuple[str, ...] = ()
        for segment in command.path[:-1]:
            parent_subparsers = subparsers_by_prefix[prefix]
            next_prefix = (*prefix, segment)
            if next_prefix not in subparsers_by_prefix:
                group_parser = parent_subparsers.add_parser(segment, help=f"{segment} commands")
                subparsers_by_prefix[next_prefix] = group_parser.add_subparsers(
                    dest="_" + "_".join(next_prefix),
                    help=f"{segment} subcommands",
                )
            prefix = next_prefix
        command_parser = subparsers_by_prefix[prefix].add_parser(
            command.path[-1],
            help=command.description,
            description=command.description,
        )
        configure_command = not command.lazy or argv is None or command.path == active_lazy_path
        if configure_command:
            command.configure(command_parser)
        if command.requires_confirmation and configure_command:
            command_parser.add_argument(
                "--yes",
                action="store_true",
                help="Confirm this mutating operation",
            )
        command_parser.set_defaults(_command_spec=command)
    return parser


def _context_for(spec: CommandSpec, root: Path) -> AppContext:
    if not spec.component_identifier:
        return AppContext(root=root)
    component = discover_components(root)[spec.component_identifier]
    return ComponentContext(
        root=root,
        stack_name=component.stack,
        component_name=component.name,
        component_dir=component.directory,
    )


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    try:
        validate_resource_root(root)
        raw_argv = list(sys.argv[1:] if argv is None else argv)
        confirmation_argv = (
            raw_argv[: raw_argv.index("--")] if "--" in raw_argv else raw_argv
        )
        for command in discover_commands(root):
            if (
                command.requires_confirmation
                and tuple(raw_argv[: len(command.path)]) == command.path
                and "--yes" not in confirmation_argv
            ):
                raise CommandError(
                    f"Command {' '.join(command.path)} requires explicit --yes confirmation"
                )
        parser = create_parser(root, raw_argv)
        args = parser.parse_args(raw_argv)
        spec = getattr(args, "_command_spec", None)
        if spec is None:
            parser.print_help()
            return 0
        if spec.requires_confirmation and not args.yes:
            raise CommandError(
                f"Command {' '.join(spec.path)} requires explicit --yes confirmation"
            )
        return int(spec.run(args, _context_for(spec, root)) or 0)
    except (
        CommandError,
        ComponentError,
        DeploymentError,
        EnvError,
        OSError,
        PresetError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
