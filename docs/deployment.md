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

## CPA v8 bootstrap and rollback

The CPA example at `components/llmproxy/cli-proxy-api/configs/config.example.yaml`
uses the v8 layout, checked against CPA v8.0.3. Use it for new v8 installations,
not as a replacement for an existing private configuration or a v7 server.
The LiteLLM integration JSON is a different schema and does not need this migration.

### Fresh bootstrap (local preparation)

1. Confirm the intended CPA image supports v8 configuration. Record/pin the intended
   image tag or digest in your deployment overrides; the base Compose image is unpinned.
2. Copy the example only if no local config exists:

   ```bash
   test ! -e components/llmproxy/cli-proxy-api/configs/config.local.yaml && \
     (umask 077; cp components/llmproxy/cli-proxy-api/configs/config.example.yaml \
       components/llmproxy/cli-proxy-api/configs/config.local.yaml)
   ```

3. Populate private settings in the ignored local file before deployment. Set a
   management secret under `management.secret-key` when using remote management;
   the empty example is not a usable remote-management credential. Client keys go
   under `access.api-keys`; upstream provider keys use the separate v8 `api-keys`
   tree. Do not commit secrets or copy the old top-level client `api-keys` list there.
4. Validate/render locally using the read-only workflow above. Deployment requires
   separate authorization and the usual live readback checks.

### Existing installation: the volume owns the active config

`compose.yaml` mounts the Docker config at `/CLIProxyAPI/config_ro/config.yaml`.
On startup, it copies this seed to `/CLIProxyAPI/config/config.yaml` **only if the
latter does not exist**. The writable file lives in `cli-proxy-api-config-data`.
Changing the repository seed or its content-addressed Docker config and redeploying
does not overwrite an existing runtime file. Do not delete the volume to force a refresh.

CPA v8.0.3 can load a purely legacy v7 configuration. A successful write through
`/v8/management/config…` migrates it to v8; a GET does not migrate it. Avoid mixed
legacy/v8 fields: new fields take precedence and legacy entries may be removed.
The `/v0/management` API remains supported. Inference URLs must not be changed to
`/v8/management`.

Before an authorized upgrade or migration:

- Identify the actual CPA task/node and volume mounts through Docker/Portainer;
  Compose volume keys are not necessarily the deployed volume names.
- Record the running image digest and service specification/overrides.
- Take protected backups of the **active** `/CLIProxyAPI/config/config.yaml`,
  `/CLIProxyAPI/auth`, and `/CLIProxyAPI/plugins` where used. Preserve ownership and
  permissions; keep copies off the volumes being changed. The repository seed is
  not a substitute for the active config backup.
- Coordinate a maintenance window and stop CPA writers while taking a consistent
  backup. Retain the original v7 config separately from any migrated v8 backup.
- After upgrade, verify the running image, task health, persisted config layout,
  auth availability, management access, and representative inference routes.
  A listening port alone does not prove routing works.

### Rollback after an authorized upgrade

1. Stop CPA tasks/writers before restoring files; prevent a v8 instance from
   rewriting the restored v7 configuration during rollback.
2. Restore the matching pre-upgrade config in the writable config volume, and the
   auth/plugin backups if they were changed incompatibly. Preserve permissions.
3. Restore the recorded v7 image digest and corresponding service settings, then
   restart only after the config and runtime version agree. Changing the image
   alone is insufficient once the persisted configuration has migrated to v8.
4. Read back the actual mounts, active config version, running image and task
   state. Verify management access and representative inference requests again.
   Keep backups until these checks pass; do not remove volumes as rollback cleanup.

Reference: [CPA v8.0.3 management API](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.3/docs/management-api-v8.md)
and [configuration example](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.3/config.example.yaml).

## Live deployment boundary

Refactor verification stops at rendering, validation, dry-run, tests, and package installation. A live deploy must be explicitly requested and then verified by reading back the exact stack, service/task state, effective environment scope, config references, networks, volumes, and health endpoints.

The current `deploy` command reports a stack update as **submitted**, not verified. Remote task health and attached config readback remain a separate mandatory operator verification step.
