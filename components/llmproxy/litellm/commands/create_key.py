"""LiteLLM API-key creation command owned by the LiteLLM component."""

from __future__ import annotations

import argparse
import os
import sys

from llmproxy.core.command import CommandError
from llmproxy.core.env import load_dotenv
from llmproxy.core.component_modules import load_component_module

COMMAND = "create-key"
ALIASES = (("key", "create"),)
DESCRIPTION = "Create a LiteLLM user and API key"
MUTATING = True

DEFAULT_KEY_RPM_LIMIT = 100
DEFAULT_KEY_MAX_BUDGET = 700
DEFAULT_KEY_BUDGET_DURATION = "7d"


def _get_api_key() -> str:
    return os.environ.get("LITELLM_API_KEY", "")


def _get_base_url() -> str:
    return os.environ.get("LITELLM_BASE_URL", "").rstrip("/")


def _require_management_env() -> None:
    missing = [
        name
        for name, value in (
            ("LITELLM_API_KEY", _get_api_key()),
            ("LITELLM_BASE_URL", _get_base_url()),
        )
        if not value
    ]
    if missing:
        raise CommandError(f"Missing management environment: {', '.join(missing)}")


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
    parser.add_argument("email", help="User email address")
    parser.add_argument("--alias", "-a", help="API key alias (default: email prefix)")
    parser.add_argument("--key", "-k", help="Custom API key value")
    parser.add_argument("--team-id", help="Optional LiteLLM team ID")
    parser.add_argument(
        "--models",
        help="Comma-separated models assigned to the user and API key",
    )
    parser.add_argument(
        "--rpm-limit",
        "--rpm",
        type=positive_int,
        default=DEFAULT_KEY_RPM_LIMIT,
    )
    parser.add_argument(
        "--max-budget",
        type=positive_float,
        default=DEFAULT_KEY_MAX_BUDGET,
    )
    parser.add_argument(
        "--budget-duration",
        default=DEFAULT_KEY_BUDGET_DURATION,
    )


def run(args, context):
    load_dotenv(context.root / ".env")
    _require_management_env()
    backend = load_component_module(context.root, "llmproxy/litellm", "src.key_create")
    api_key = backend.create_key_for_user(
        args.email,
        base_url=_get_base_url(),
        api_key=_get_api_key(),
        alias=args.alias,
        key_value=args.key,
        rpm_limit=args.rpm_limit,
        max_budget=args.max_budget,
        budget_duration=args.budget_duration,
        team_id=args.team_id,
        models=args.models.split(",") if args.models else None,
        report=lambda message: print(message, file=sys.stderr),
    )
    print(api_key)
    return 0


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    configure(parser)
    args = parser.parse_args(argv)

    class Context:
        from pathlib import Path
        root = Path(__file__).resolve().parents[4]

    return run(args, Context())


if __name__ == "__main__":
    raise SystemExit(main())
