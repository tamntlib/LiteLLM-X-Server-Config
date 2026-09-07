# Architecture

## Design goals

The repository uses component-first Vertical Slice Architecture for an I/O-heavy operations CLI.

- A service and its deployment/configuration artifacts live together.
- Adding a service does not require editing a central dispatcher or registry.
- Presets select components explicitly; filesystem discovery never implies production activation.
- Generic orchestration remains in `llmproxy/commands/`.
- Component-specific operations remain under the owning component.
- Rendered production topology and content-addressed config names remain stable through refactors.

## Ownership boundaries

### `components/`

Owns both levels of the architecture:

- Optional `components/<stack>/compose.yaml` and optional `stack.toml` contain stack-level base resources and ordering metadata, respectively. A stack with only component-owned resources needs no base Compose file or empty placeholder.
- `components/<stack>/<component>/` contains service Compose fragments, overlays, config layers, Docker config sources, commands, libraries, schemas, public/private data, and colocated tests.

Stack names remain stable so service, network, volume, and config ownership does not drift. Stack metadata is not a component and is not a central registry.

When present, the stack base is merged first, followed by selected component Compose files, feature overlays, and component local overrides in their existing selection order. A missing base is skipped, with no fallback to retired roots. An existing base must be a regular file: directories, FIFOs, and all symlinks (including dangling or same-owner links) are rejected.

### LiteLLM ownership

LiteLLM implementation, base configuration, schemas, and key rules live under `components/llmproxy/litellm/`:

```text
configs/                        # declarative inputs and adjacent JSON schemas
├── litellm.yaml                 # runtime configuration
├── config.json
├── config.schema.json
├── key-limits.json
├── key-limits.schema.json
└── key-limits.local.json        # ignored private override
src/                            # backend logic, independent of CLI context/argparse
├── __init__.py
├── config_generate.py
├── config_sync.py
├── key_create.py
└── key_limits.py
commands/                       # thin config/key CLI adapters only
tests/
├── test_cli.py
├── test_config_generator.py
├── test_config_syncer.py
├── test_keys.py
└── test_key_create.py
```

This layout groups files by role, not by operation: `configs/` accepts runtime YAML or management config JSON and keeps schemas beside their JSON inputs. `src/` owns generation, synchronization, and key policy without `argparse` or CLI context. `commands/` adapts arguments/context to backend calls and maps results/errors to the CLI. Optional directories are created only for actual files, never as empty placeholders.

Other components retain source ownership of their contributions under `integrations/llmproxy/litellm/config.json` and the ignored sibling `config.local.json`. The destination identifier is explicit in the directory path, so no manifest mapping is needed. The LiteLLM owner interprets and merges these files according to preset selection; ownership does not transfer to the generic core.

### `presets/`

Owns explicit topology selection and inheritance. Presets are declarative; they never invoke arbitrary Python actions.

The operator-facing `.env` also remains at the repository root; it is not moved into a component.

### `llmproxy/core/`

Owns service-independent command/context contracts, environment handling, HTTP/process helpers, JSON merging, resource handling, component library loading, and destination-namespaced integration path lookup. It does not own LiteLLM generators, management API policy, key rules, or schemas.

Load component libraries through `llmproxy.core.component_modules.load_component_module(root, identifier, module_name)`, or through `context.load_module(module_name)` inside a component command. The loader resolves the owning component under the selected checkout or packaged resource root. Component libraries are not global service-specific Python packages and have no compatibility shims; callers must not hard-code a filesystem path into `sys.path`.

### `llmproxy/deployment/`

Discovers components, resolves presets, assembles Compose inputs, performs preflight checks, renders stacks, computes content-addressed configs, and drives deployment.

### `llmproxy/commands/`

Owns generic command slices such as `components`, `presets`, `render`, `validate`, `inputs`, and `deploy`. LiteLLM config operations are component-owned, not generic orchestration.

### `components/*/*/commands/`

Owns thin component-specific CLI adapters, not backend policy. The CLI reads literal command metadata without executing the module, then imports only the explicitly selected component command and registers the canonical path:

```text
llmproxy <stack>/<component> <command> [<subcommand> ...]
```

The full `<stack>/<component>` identifier avoids collisions between same-named components in different stacks. The retired `component` prefix has no compatibility alias. A global alias is registered only when declared and collision-free; existing `key create` and `key limits` aliases are preserved.

The `litellm` component in the `llmproxy` stack owns config generation and synchronization:

| Command file | Literal metadata | Canonical command |
|---|---|---|
| `components/llmproxy/litellm/commands/config_generate.py` | `COMMAND = ("config", "generate")` | `uv run llmproxy llmproxy/litellm config generate` |
| `components/llmproxy/litellm/commands/config_sync.py` | `COMMAND = ("config", "sync")` | `uv run llmproxy llmproxy/litellm config sync` |

The root `config` command namespace is removed with no alias. Command adapters load `src.config_generate`, `src.config_sync`, `src.key_create`, and `src.key_limits` from their component through the generic loader. The shared base is `components/llmproxy/litellm/configs/config.json`; the default output remains `build/llmproxy/litellm/config.gen.json`. CLI namespaces, aliases, options, custom `--config`/`--output` paths, and preset-based layer selection are unchanged by this role-based layout. See the classified old-to-new path/import table in [Migration](migration.md).

## Discovery contract

From `components/<stack>/<component>/`, the CLI infers:

- stack and component identifiers;
- `compose.yaml`;
- service names from the Compose `services` mapping;
- `overlays/<feature>.yaml`;
- `compose.local.yaml`;
- `commands/*.py`.

`component.toml` is optional and must not repeat inferable values.

Service-specific configuration discovery is not part of the generic component model. `llmproxy.core.integrations.resolve_integration_file(component_directory, target, filename)` resolves a regular file under the source component's `integrations/<target-stack>/<target-component>/` namespace. Invalid identifiers, traversal, symlinks, and nonregular entries are rejected; absent files are optional. This helper neither parses content nor interprets the destination schema.

LiteLLM's generator requests destination `llmproxy/litellm`, first `config.json` from every selected source, then `config.local.json` from every selected source when local layers are enabled. Other target namespaces and unselected sources are not consulted. A target namespace does not add a deployment dependency or select a component automatically. Generic deployment discovery does not interpret integration files.

## Safety boundaries

- A component is deployed only when selected by a preset.
- Components may declare mandatory `required_features`; incompatible presets are rejected before rendering.
- Component commands are never auto-run during deploy.
- Local files are owner-scoped and merged after public fragments.
- `--stack` scopes preflight, config creation, rendering, deployment, and verification without redefining the preset.
- Docker service removals require `--allow-remove-services`, which controls `--prune`.
- Portainer config inventory failures stop deployment; an existing content-addressed config is reused.
- Management API failures return non-zero and do not report success.

## Build resources

`hatch_build.py` uses only `components/` and `presets/` as public resource roots, alongside the generic `llmproxy` Python package. Optional stack files and component commands, libraries, schemas, and data are therefore packaged together by convention. Private `*.local*` files and generated artifacts are excluded, with one public leaf-name exception: regular `compose.local.example.yaml` files outside private `.local` directories are packaged as copyable templates, never automatically merged. Adding a public component does not require editing package metadata. Editable installs keep generic Python modules linked to checkout source rather than copying stale modules into `site-packages`.

## Test ownership and discovery

Generic CLI, deployment, loader, HTTP, and packaging tests remain under root `tests/`. LiteLLM-specific CLI, generator, sync, key-limit, and key-creation tests are colocated under `components/llmproxy/litellm/tests/`. Generic CLI tests do not import or copy service backends into their fixtures. `tests/test_components.py` aggregates component tests into the normal root discovery run, so moving a test does not remove it from the suite:

```bash
taskset -c 0,1 uv run --with 'hatchling>=1.27,<2' --with editables python3 -m unittest discover -s tests -p 'test_*.py'
```

Hatchling and `editables` are ephemeral packaging-test dependencies supplied by `uv run --with`, not runtime dependencies.
