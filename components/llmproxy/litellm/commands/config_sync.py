from __future__ import annotations

import asyncio
from pathlib import Path

from llmproxy.core.component_modules import load_component_module

COMMAND = ("config", "sync")
DESCRIPTION = "Synchronize resolved configuration with the LiteLLM API"
MUTATING = True


def configure(parser):
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--preset")
    source.add_argument("--config", type=Path)
    parser.add_argument(
        "--only",
        default="credentials,models,aliases,fallbacks,public_model_hub,router_settings,guardrails",
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--prune", action="store_true")
    parser.add_argument("--dry-run", action="store_true")


def run(args, context):
    sync = load_component_module(context.root, "llmproxy/litellm", "src.config_sync")
    return asyncio.run(
        sync.sync_config(
            config_path=args.config,
            preset=args.preset,
            only=args.only,
            force=args.force,
            prune=args.prune,
            dry_run=args.dry_run,
            root=context.root,
        )
    ) or 0
