from __future__ import annotations


def add_selection_arguments(parser):
    parser.add_argument("--preset", required=True, help="Preset name")
    parser.add_argument("--stack", help="Operate on one stack selected by the preset")
    parser.add_argument(
        "--no-local-overrides",
        action="store_true",
        help="Disable component and stack local Compose overrides",
    )
