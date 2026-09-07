# Components

## Minimum contract

A component requires only:

```text
components/<stack>/<component>/compose.yaml
```

Example:

```yaml
services:
  worker:
    image: example/worker:stable
```

The path identifies the component as `<stack>/<component>`, and service names are read from Compose.

Stack-level files live one level above the component: optional `components/<stack>/compose.yaml` contains shared base resources, and optional `components/<stack>/stack.toml` supplies ordering metadata. Neither is required to render a selected component; do not create an empty `services: {}` placeholder. They are not component fragments and do not select components; root `presets/` retains that responsibility. If a stack base exists, it must be a regular file, not a directory, FIFO, or symlink (even a dangling or same-owner symlink). Retired roots are never fallbacks; see [Migration](migration.md) for historical paths.

`components/portainer/portainer/` intentionally owns the complete historical Portainer stack as one component: `traefik`, `agent`, and `portainer`, together with their networks and volumes. This preserves their stack-level routing and ownership contract while keeping selection independent from `all`.

## Optional files

| Path | Purpose |
|---|---|
| `component.toml` | Non-inferable dependencies, required inputs, Docker config naming |
| `integrations/<target-stack>/<target-component>/` | Source-owned contributions addressed to a destination component |
| `integrations/llmproxy/litellm/config.json` | LiteLLM contribution merged when the source component is selected |
| `integrations/llmproxy/litellm/config.local.json` | Ignored local contribution merged only when the source component is selected |
| `overlays/<feature>.yaml` | Compose merged only when the preset enables the feature |
| `configs/` | Declarative runtime YAML or management config JSON, examples, and schemas beside their JSON files |
| `compose.local.yaml` | Ignored machine/site structural override |
| `compose.local.example.yaml` | Public template; copy to sibling `compose.local.yaml` to use it, never automatically merged |
| `src/` | Component-owned backend logic; no `argparse` or CLI context dependency |
| `commands/*.py` | Dynamically discovered thin CLI adapters; translate arguments/context and map backend results/errors |
| `tests/` | Colocated component tests |

These directories are role-based ownership conventions, not mandatory folders for every service; create no empty placeholders. LiteLLM owns `configs/{litellm.yaml,config.json,config.schema.json,key-limits.json,key-limits.schema.json}` and ignored `configs/key-limits.local.json` under `components/llmproxy/litellm/`. Its backend package is `src/{__init__.py,config_generate.py,config_sync.py,key_create.py,key_limits.py}`. Other components keep their contributions under their own `integrations/llmproxy/litellm/` directory. Load libraries with `context.load_module(...)` or the generic `llmproxy.core.component_modules.load_component_module(root, identifier, module_name)` API, not a global service-specific package. LiteLLM module names are `src.config_generate`, `src.config_sync`, `src.key_create`, and `src.key_limits`.

The integration path identifies the destination by its full `<stack>/<component>` identifier; do not repeat that mapping in `component.toml`. A destination owns the filenames, schema, and merge behavior it consumes. The generic core only resolves a requested file safely; discovering a component does not parse its integrations or automatically enable the destination.

LiteLLM's `tests/` owns `test_cli.py`, `test_config_generator.py`, `test_config_syncer.py`, `test_keys.py`, and `test_key_create.py`. Root `tests/test_components.py` aggregates component tests into the standard root unittest discovery run.

## `component.toml`

Use it only when the CLI must not guess. Declared file paths must remain inside the owning component directory:

```toml
requires = ["llmproxy-data/postgres"]
required_features = ["traefik"]
required_files = ["configs/litellm.yaml"]
required_environment = ["DB_PASSWORD"]

[[docker_configs]]
source = "configs/litellm.yaml"
name = "llmproxy_litellm-config-yaml"
environment = "LITELLM_CONFIG_NAME"
```

Do not add redundant fields such as component name, stack, Compose path, service list, overlay list, or config-layer path.

Use `required_features` only when a stable, zero-drift base topology already depends on that feature. Presets selecting the component without all mandatory features are rejected.

## Local ownership

Use `compose.local.yaml` for structural machine-specific changes such as mounts, placement, resources, commands, or labels. Values and secrets belong in root `.env` or another ignored `*.local.*` file owned by the component.

Merge order is deterministic:

1. optional stack base, if present;
2. selected component Compose files, in resolved preset component order;
3. selected feature overlays, in resolved feature order then component order;
4. selected component local overrides, in component order.

Unselected component local files are never merged.
