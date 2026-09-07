# Adding a component

## Minimal component

Create one file:

```text
components/<stack>/<component>/compose.yaml
```

```yaml
services:
  new-service:
    image: example/new-service:stable
```

Then explicitly select it in a preset:

```toml
components = ["<stack>/<component>"]
```

A component is discoverable immediately:

```bash
uv run llmproxy components
```

It is not deployed until a preset selects it.

## Stack-level files

Both stack-level files are optional. When stack-wide base resources or ordering are needed, colocate them above its components:

```text
components/<stack>/compose.yaml
components/<stack>/stack.toml
```

Keep service-specific resources under the component. Stack files are not a separate registry or a preset; root `presets/` still selects topology and root `.env` still owns operator environment inputs.

Do not add an empty `services: {}` base just to make a new stack render. A component's Compose file and an explicit preset selection are sufficient. If shared resources are needed, a base can contain, for example:

```yaml
networks:
  shared:
    external: true
```

The optional base is merged first, before selected component Compose files, feature overlays, and local overrides; those layers retain their selection order. A missing base is skipped. An existing base must be a regular file; directories, FIFOs, and symlinks (including dangling and same-owner links) fail closed. Retired roots are ignored, not used as fallbacks; see [Migration](migration.md) for historical paths.

## Optional feature overlay

```text
components/<stack>/<component>/overlays/traefik.yaml
```

The overlay is merged only for a resolved preset containing:

```toml
features = ["traefik"]
```

## Optional LiteLLM layer

Add:

```text
components/<stack>/<component>/integrations/llmproxy/litellm/config.json
```

`uv run llmproxy llmproxy/litellm config generate` and `uv run llmproxy llmproxy/litellm config sync` automatically include it whenever the preset selects the contributing component. The commands remain owned by stack `llmproxy`, component `litellm`, even when another component contributes the layer.

Use sibling `config.local.json` for ignored operator overrides. All selected public contributions are merged before all selected local contributions. The source component owns these files; the destination owns their schema and interpretation. For another destination, use `integrations/<target-stack>/<target-component>/` and its documented file contract. No integration mapping belongs in `component.toml`, and a directory alone does not activate a destination or create a deployment dependency.

## Optional metadata

Create `component.toml` only for information that cannot be inferred safely:

```toml
requires = ["other-stack/dependency"]
required_files = ["configs/config.local.yaml"]
required_environment = ["SERVICE_TOKEN"]

[[docker_configs]]
source = "configs/config.local.yaml"
name = "stack_service-config"
environment = "SERVICE_CONFIG_NAME"
```

Do not repeat stack, component, Compose, services, overlays, config layer, or commands.

## Optional command

```text
components/<stack>/<component>/commands/status.py
```

Implement the command contract documented in [Component commands](component-commands.md). No dispatcher edit is required.

## Owned libraries, data, and tests

Keep service-specific files inside the component, grouped by role rather than by operation:

- `configs/`: declarative inputs, whether runtime YAML or management config JSON; keep schemas beside their JSON files.
- `src/`: backend logic without `argparse` or CLI context; include `__init__.py` for the Python package.
- `commands/`: thin CLI adapters that declare metadata/options, translate arguments/context into explicit backend inputs, and map results/errors to CLI output and status.

For example, `components/llmproxy/litellm/` owns `configs/{litellm.yaml,config.json,config.schema.json,key-limits.json,key-limits.schema.json}` and ignored `configs/key-limits.local.json`. Its backend package is `src/{__init__.py,config_generate.py,config_sync.py,key_create.py,key_limits.py}`. Create optional directories only when they contain real files; a minimal component does not need empty folders for any of these roles. Source-owned destination contributions remain in `integrations/llmproxy/litellm/`, not in the destination's `configs/`.

Commands load an owned library with `context.load_module("src.config_generate")`; LiteLLM's other backend module names are `src.config_sync`, `src.key_create`, and `src.key_limits`. Other callers use `llmproxy.core.component_modules.load_component_module(root, identifier, module_name)`. Generic CLI/context, environment, HTTP, resource, and loading infrastructure stays in `llmproxy/core/`; do not add compatibility shims or service-specific imports there.

Put component tests in `components/<stack>/<component>/tests/test_*.py`. `tests/test_components.py` aggregates them into normal root unittest discovery, alongside generic infrastructure tests. LiteLLM's colocated suite covers its CLI behavior, generation, synchronization, key limits, and key creation.

## Validation checklist

Set `PRESET` to the preset that explicitly selects the component before running this checklist.

```bash
uv run llmproxy components
uv run llmproxy inputs --preset "$PRESET"
uv run llmproxy validate --preset "$PRESET"
uv run llmproxy render --preset "$PRESET" --no-local-overrides
taskset -c 0,1 uv run --with 'hatchling>=1.27,<2' --with editables python3 -m unittest discover -s tests -p 'test_*.py'
taskset -c 0,1 uv build
```

Hatchling and `editables` are ephemeral packaging-test dependencies, not runtime dependencies.

The build hook discovers public files under the resource roots `components/` and `presets/` automatically, including stack files and component libraries/data; `pyproject.toml` does not require a per-component entry. Ignored private/local files and generated outputs are excluded.
