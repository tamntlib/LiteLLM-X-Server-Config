from __future__ import annotations

import json
from pathlib import Path

from llmproxy.core.command import CommandError
from llmproxy.core.component_modules import load_component_module
from llmproxy.core.resources import write_private_text

COMMAND = ("config", "generate")
DESCRIPTION = "Generate resolved LiteLLM configuration"
MUTATING = False


def configure(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--preset")
    source.add_argument("--config", type=Path)
    parser.add_argument("--output", "-o", type=Path)


def run(args, context):
    generator = load_component_module(context.root, "llmproxy/litellm", "src.config_generate")
    output = args.output or context.root / generator.DEFAULT_OUTPUT_FILE
    if args.config:
        if not args.config.is_file():
            raise CommandError(f"Config file not found: {args.config}")
        config = generator.generate_config(args.config, require_complete=True)
    else:
        config = generator.generate_config_for_preset(
            args.preset,
            require_complete=True,
            root=context.root,
        )
    output = write_private_text(
        output,
        json.dumps(config, indent=4) + "\n",
        confinement_root=context.root if args.output is None else None,
    )
    print(f"Generated config written to: {output}")
    return 0
