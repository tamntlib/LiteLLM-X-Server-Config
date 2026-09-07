from __future__ import annotations

from llmproxy.deployment.discovery import discover_components

COMMAND = "components"
DESCRIPTION = "List deployment components"
MUTATING = False


def configure(parser):
    pass


def run(args, context):
    for component in discover_components(context.root).values():
        services = ", ".join(component.services)
        print(f"{component.identifier}: {services}")
    return 0
