from __future__ import annotations

from llmproxy.commands._selection import add_selection_arguments
from llmproxy.deployment.compose import compose_files_for_preset

COMMAND = "inputs"
DESCRIPTION = "Show Compose merge inputs"
MUTATING = False


def configure(parser):
    add_selection_arguments(parser)


def run(args, context):
    files = compose_files_for_preset(
        args.preset,
        stack=args.stack,
        include_local_overrides=not args.no_local_overrides,
        root=context.root,
    )
    for stack_name, paths in files.items():
        print(f"{stack_name}:")
        for path in paths:
            try:
                display = path.relative_to(context.root)
            except ValueError:
                display = path
            suffix = " [local]" if ".local." in path.name else ""
            print(f"  {display}{suffix}")
    return 0
