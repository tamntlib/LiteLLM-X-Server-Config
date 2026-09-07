from __future__ import annotations

from pathlib import Path

from llmproxy.deployment.deploy import deploy_preset

COMMAND = "deploy"
DESCRIPTION = "Deploy a preset through Docker Swarm or Portainer"
MUTATING = True


def configure(parser):
    parser.add_argument("--preset", required=True, help="Preset name")
    parser.add_argument("--stack", help="Deploy one stack selected by the preset")
    parser.add_argument("--driver", choices=("docker", "ptctools"), default="docker")
    parser.add_argument("--docker-context")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-remove-services", action="store_true")
    parser.add_argument("--no-local-overrides", action="store_true")


def run(args, context):
    commands = deploy_preset(
        args.preset,
        stack=args.stack,
        driver=args.driver,
        docker_context=args.docker_context,
        env_file=args.env_file,
        dry_run=args.dry_run,
        allow_remove_services=args.allow_remove_services,
        include_local_overrides=not args.no_local_overrides,
        root=context.root,
    )
    if args.dry_run:
        for command in commands:
            print(command)
    else:
        scope = f"stack '{args.stack}'" if args.stack else f"preset '{args.preset}'"
        print(
            f"Submitted {scope} through {args.driver}; "
            "remote task health and config attachments are not verified"
        )
    return 0
