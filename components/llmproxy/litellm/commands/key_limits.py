#!/usr/bin/env python3
import argparse
import os
import sys
from pathlib import Path

from llmproxy.core.command import CommandError
from llmproxy.core.component_modules import load_component_module
from llmproxy.core.env import load_dotenv


COMMAND = "key-limits"
ALIASES = (("key", "limits"),)
DESCRIPTION = "Set default limits for existing LiteLLM API keys"
MUTATING = True


def _get_api_key() -> str:
    return os.environ.get("LITELLM_API_KEY", "")


def _get_base_url() -> str:
    return os.environ.get("LITELLM_BASE_URL", "").rstrip("/")


DEFAULT_KEY_RPM_LIMIT = 100
DEFAULT_KEY_MAX_BUDGET = 700
DEFAULT_KEY_BUDGET_DURATION = "7d"
COMPONENT_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = COMPONENT_DIR.parents[2]
MAX_PAGE_SIZE = 100


def positive_int(value):
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def positive_float(value):
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive number") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive number")
    return parsed


def configure(parser):
    parser.add_argument(
        "--rpm-limit",
        "--rpm",
        type=positive_int,
        default=DEFAULT_KEY_RPM_LIMIT,
        help=f"Default requests per minute (default: {DEFAULT_KEY_RPM_LIMIT})",
    )
    parser.add_argument(
        "--max-budget",
        type=positive_float,
        default=DEFAULT_KEY_MAX_BUDGET,
        help=f"Default max budget (default: {DEFAULT_KEY_MAX_BUDGET:g})",
    )
    parser.add_argument(
        "--budget-duration",
        default=DEFAULT_KEY_BUDGET_DURATION,
        help=f"Budget reset duration (default: {DEFAULT_KEY_BUDGET_DURATION})",
    )
    parser.add_argument("--page-size", type=positive_int, default=MAX_PAGE_SIZE)
    parser.add_argument("--reset-spend", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", "--yes", dest="apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")


def run(args, context):
    load_dotenv(context.root / ".env")
    if not _get_api_key() or not _get_base_url():
        missing = [
            name
            for name, value in (
                ("LITELLM_API_KEY", _get_api_key()),
                ("LITELLM_BASE_URL", _get_base_url()),
            )
            if not value
        ]
        raise CommandError(f"Missing management environment: {', '.join(missing)}")
    backend = load_component_module(context.root, "llmproxy/litellm", "src.key_limits")
    return backend.set_key_limits(
        base_url=_get_base_url(),
        api_key=_get_api_key(),
        config_path=context.component_dir / "configs" / "key-limits.json",
        rpm_limit=args.rpm_limit,
        max_budget=args.max_budget,
        budget_duration=args.budget_duration,
        page_size=args.page_size,
        reset_spend=args.reset_spend,
        apply=args.apply,
        report=lambda message: print(message, file=sys.stderr),
    )


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    configure(parser)
    args = parser.parse_args(argv)

    class Context:
        root = REPO_ROOT
        component_dir = COMPONENT_DIR

    return run(args, Context())


if __name__ == "__main__":
    raise SystemExit(main())
