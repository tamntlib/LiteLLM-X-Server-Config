from __future__ import annotations

from llmproxy.commands._selection import add_selection_arguments
from llmproxy.deployment.compose import validate_preset

COMMAND = "validate"
DESCRIPTION = "Validate selected component stacks"
MUTATING = False


def configure(parser):
    add_selection_arguments(parser)


def run(args, context):
    validate_preset(
        args.preset,
        stack=args.stack,
        include_local_overrides=not args.no_local_overrides,
        root=context.root,
    )
    print(f"Preset '{args.preset}' is valid")
    return 0
