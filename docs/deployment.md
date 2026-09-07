# Deployment

## Read-only workflow

```bash
uv run llmproxy components
uv run llmproxy presets
uv run llmproxy inputs --preset default
uv run llmproxy validate --preset default
uv run llmproxy render --preset default
uv run llmproxy deploy --preset default --dry-run
```

These commands inspect, validate, or generate local files. They do not update a live stack.

The Portainer service stack is selected independently and is not part of `all`:

```bash
uv run llmproxy inputs --preset portainer
uv run llmproxy validate --preset portainer
uv run llmproxy render --preset portainer --no-local-overrides
uv run llmproxy deploy --preset portainer --driver docker --dry-run
```

That dry-run emits one deploy command for the `portainer` stack. Running it without `--dry-run` is a separate live operation and is not part of the source migration.

## Stack selection

`--stack` narrows one operation without changing preset topology:

```bash
uv run llmproxy deploy --preset default --stack llmproxy --dry-run
```

The preset still selects features and component config layers. Dependency stacks are not deployed implicitly and must already exist.

## Docker driver

```bash
uv run llmproxy deploy --preset default --driver docker
```

Omitted services are removed only when explicitly authorized:

```bash
uv run llmproxy deploy \
  --preset default \
  --driver docker \
  --allow-remove-services
```

That flag controls Docker `--prune`; it is not only a preflight bypass.

To render the complete production topology without Headroom:

```bash
uv run llmproxy inputs --preset default
uv run llmproxy validate --preset default
uv run llmproxy deploy --preset default --driver docker --dry-run
```

`default` excludes the complete `llmproxy/headroom` component, not only its
Compose service. A live transition from `all` removes the `headroom` service only when
Docker deployment is explicitly authorized with `--allow-remove-services`, which maps to
`docker stack deploy --prune`. The `headroom-data` volume is omitted from the new manifest
but is not automatically deleted by stack deployment. Review the service and route removal
separately before deployment; no live transition is performed by these verification steps.

## Portainer/ptctools driver

```bash
uv run llmproxy deploy \
  --preset default \
  --stack llmproxy \
  --driver ptctools \
  --allow-remove-services \
  --dry-run
```

`ptctools` cannot provide the same pre-update remote service inventory, so the risk acknowledgement is mandatory. Application environment is written to a mode-`0600` temporary file scoped to the selected stack. Portainer credentials stay in the wrapper process environment; unrelated application secrets are excluded.

Before creating a content-addressed Docker config, the driver queries the exact target name. Existing configs are reused. Not-found permits creation; authentication, connection, or malformed-response errors stop deployment.

## Stable config names

The compatibility names include:

```text
llmproxy_litellm-config-yaml-700697bb9bb1
llmproxy_cli-proxy-api-config-yaml-39f74aed8448
```

The suffix changes only when source bytes change.

## Local overrides

Disable ignored component local files for a public-only render or deployment:

```bash
uv run llmproxy render --preset default --no-local-overrides
uv run llmproxy deploy --preset default --no-local-overrides --dry-run
```

Rendered files use mode `0600` because selected local overrides may contain sensitive values.

## Live deployment boundary

Refactor verification stops at rendering, validation, dry-run, tests, and package installation. A live deploy must be explicitly requested and then verified by reading back the exact stack, service/task state, effective environment scope, config references, networks, volumes, and health endpoints.

The current `deploy` command reports a stack update as **submitted**, not verified. Remote task health and attached config readback remain a separate mandatory operator verification step.
