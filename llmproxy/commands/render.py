from __future__ import annotations

from pathlib import Path

from llmproxy.commands._selection import add_selection_arguments
from llmproxy.deployment.compose import render_preset

COMMAND = "render"
DESCRIPTION = "Render selected component stacks"
MUTATING = False


def configure(parser):
    add_selection_arguments(parser)
    parser.add_argument("--output-dir", type=Path)


def run(args, context):
    outputs = render_preset(
        args.preset,
        output_dir=args.output_dir,
        stack=args.stack,
        include_local_overrides=not args.no_local_overrides,
        root=context.root,
    )
    for path in outputs.values():
        try:
            print(path.relative_to(context.root))
        except ValueError:
            print(path)
    return 0
