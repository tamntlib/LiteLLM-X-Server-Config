from __future__ import annotations

from llmproxy.deployment.preset import list_presets

COMMAND = "presets"
DESCRIPTION = "List deployment presets"
MUTATING = False


def configure(parser):
    pass


def run(args, context):
    for preset in list_presets(context.root):
        print(f"{preset.name}: {preset.description}")
    return 0
