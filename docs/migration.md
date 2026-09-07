# Migration to component-first layout

## Historical old-to-new paths and imports

The **previous** column is historical migration evidence, not an active path, import, or command. Current targets below reflect the role-based layout, including for earlier migrations; historical sources are preserved. Retired locations have no compatibility shims. Unchanged shared resources and component contributions are explicitly classified below.

| Classification | Previous path, import, or responsibility | Current owner/location |
|---|---|---|
| Retired root; stack files optional and colocated | `stacks/<stack>/compose.yaml`, `stacks/<stack>/stack.toml` | Optional `components/<stack>/compose.yaml`, optional `components/<stack>/stack.toml` |
| Retired service/module layout | `deploy/modules/` | `components/<stack>/<component>/compose.yaml` |
| Retired integration layout | `deploy/integrations/` | `components/<stack>/<component>/overlays/<feature>.yaml` |
| Retired registry | `deploy/registry.json` | Filesystem discovery plus optional colocated `component.toml` |
| Retired package | `llmproxy.stack` | Generic `llmproxy.deployment` orchestration |
| Preset format migrated | JSON presets | Root `presets/*.toml` with inheritance |
| Preset renamed without topology changes | `presets/all-no-headroom.toml`, `--preset all-no-headroom` | `presets/default.toml`, `--preset default`; no old-name alias or implicit CLI selection |
| Retired root data directory; base/schema moved | `config/litellm/config.json`, `config/litellm/config.schema.json` | `components/llmproxy/litellm/configs/config.json`, `components/llmproxy/litellm/configs/config.schema.json` |
| Retired global service library | `llmproxy/configuration/{__init__.py,generator.py}`; import `llmproxy.configuration.generator` | `components/llmproxy/litellm/src/{__init__.py,config_generate.py}`; load component module `src.config_generate` |
| Retired global service library | `llmproxy/management/{__init__.py,sync.py}`; import `llmproxy.management.sync` | `components/llmproxy/litellm/src/{__init__.py,config_sync.py}`; load component module `src.config_sync` |
| Retired root data directory; rules/schema/private override moved | `config/keys/{limits.json,limits.schema.json,limits.local.json}` | `components/llmproxy/litellm/configs/{key-limits.json,key-limits.schema.json,key-limits.local.json}`; private override remains ignored |
| Retired component operation-based data layout | `components/llmproxy/litellm/configuration/{config.json,config.schema.json}` | `components/llmproxy/litellm/configs/{config.json,config.schema.json}` |
| Retired component operation-based generator package | `components/llmproxy/litellm/configuration/{__init__.py,generator.py}`; module `configuration.generator` | `components/llmproxy/litellm/src/{__init__.py,config_generate.py}`; module `src.config_generate` |
| Retired component operation-based sync package | `components/llmproxy/litellm/management/{__init__.py,sync.py}`; module `management.sync` | `components/llmproxy/litellm/src/{__init__.py,config_sync.py}`; module `src.config_sync` |
| Retired component key-data layout; filenames made explicit | `components/llmproxy/litellm/keys/{limits.json,limits.schema.json,limits.local.json}` | `components/llmproxy/litellm/configs/{key-limits.json,key-limits.schema.json,key-limits.local.json}`; private override remains ignored |
| Backend responsibility extracted; command paths retained as adapters | Key-creation and key-limit policy in component command modules | `components/llmproxy/litellm/src/{key_create.py,key_limits.py}`; modules `src.key_create`, `src.key_limits`; thin adapters remain in `commands/` |
| Retired generic command slice | `llmproxy/commands/config/` | `components/llmproxy/litellm/commands/config_generate.py` and `config_sync.py` |
| Retired root command namespace | `llmproxy config generate`, `llmproxy config sync` | `llmproxy llmproxy/litellm config generate`, `llmproxy llmproxy/litellm config sync` |
| Retired component prefix; no compatibility alias | `llmproxy component <stack>/<component> ...` | `llmproxy <stack>/<component> ...` |
| Owner-scoped default output | `build/config.gen.json` | `build/llmproxy/litellm/config.gen.json` |
| Component tests relocated | `tests/test_config_generator.py`, `tests/test_config_syncer.py`, `tests/test_keys.py`, `tests/test_key_create.py` | Same filenames under `components/llmproxy/litellm/tests/`, aggregated by root `tests/test_components.py` |
| Component CLI tests extracted | LiteLLM-specific tests and backend fixtures in `tests/test_cli.py` | `components/llmproxy/litellm/tests/test_cli.py`; generic CLI tests remain at root |
| Legacy component scripts retired | `litellm_scripts/` | LiteLLM-owned commands, libraries, schemas, data, and tests under `components/llmproxy/litellm/` |
| Legacy Portainer manifest moved | `portainer/portainer.yaml` | `components/portainer/portainer/compose.yaml`, selected by `presets/portainer.toml` |
| Legacy Portainer dotenv consolidated | `portainer/.env.example`, `portainer/.env` | Portainer section in root `.env.example` and private root `.env` |
| Source-owned contribution namespaced by destination | `components/<stack>/<component>/litellm-config.json` and ignored `litellm-config.local.json` | `components/<stack>/<component>/integrations/llmproxy/litellm/config.json` and ignored sibling `config.local.json` |
| Unchanged by role-based layout | Source-owned `integrations/llmproxy/litellm/{config.json,config.local.json}` | Same destination namespace under each source component; private sibling remains ignored |
| Unchanged runtime input | `components/llmproxy/litellm/configs/litellm.yaml` | Same component-owned runtime YAML |
| Unchanged shared ownership | Root `.env`, `presets/` | Remain root resources |
| Unchanged generic command ownership | `llmproxy/commands/` | Service-independent `components`, `presets`, `inputs`, `validate`, `render`, and `deploy` slices |

The retired root stack/data directories and global LiteLLM library directories are not resource roots or fallback locations. Only `components/` and `presets/` are packaged as public resource roots. The generic Python package remains separate from component-owned Python libraries.

## Optional stack Compose bases

Stacks no longer require `components/<stack>/compose.yaml`. The four stack-level bases for `llmproxy`, `llmproxy-data`, `monitoring`, and `portainer` contained exactly `services: {}` and have been removed; component Compose files and stack ordering metadata remain unchanged. No empty placeholder is needed when components own all resources.

Keep a nonempty stack base when it defines genuinely shared resources, such as a network used by multiple components. When present it is still the first merge input, followed by selected component Compose files, feature overlays, and component local overrides in the same order as before. Absence is allowed, but an existing directory, FIFO, or symlink (including dangling or same-owner links) is rejected. A missing base never causes a fallback to a retired root.

## Component ownership and CLI migration

Remove only the retired `component` token from component command invocations in scripts and shell history. Keep the full `<stack>/<component>` identifier, command path, and options unchanged. The full identifier avoids collisions between same-named components across stacks. There is no compatibility alias for the retired prefix; existing global `llmproxy key create` and `llmproxy key limits` aliases remain supported.

For an existing checkout, refresh only the project package before using the new namespace:

```bash
uv sync --locked --inexact --reinstall-package llmproxy
```

Older editable builds copied Python modules into `site-packages`, so the console script could run stale code even when `python -m llmproxy` used the current checkout. The build hook now leaves editable Python modules linked to source. Reinstalling once removes those stale copies; `--locked` preserves the lockfile and `--inexact` retains unrelated installed packages. This changes only the local Python environment, not live services.

Config generation and synchronization belong to stack `llmproxy`, component `litellm`. Use:

```bash
uv run llmproxy llmproxy/litellm config generate --preset default
uv run llmproxy llmproxy/litellm config sync --preset default --dry-run
```

The wrappers live in `components/llmproxy/litellm/commands/config_generate.py` and `components/llmproxy/litellm/commands/config_sync.py`, declaring `COMMAND = ("config", "generate")` and `COMMAND = ("config", "sync")` respectively. No root config alias is provided.

This migration includes backend ownership, not only CLI wrappers. Under `components/llmproxy/litellm/`, `configs/` holds declarative inputs: runtime `litellm.yaml`, management `config.json` with adjacent `config.schema.json`, and `key-limits.json` with adjacent `key-limits.schema.json` plus ignored `key-limits.local.json`. The management base keeps the filename `config.json`.

The `src/` package contains `__init__.py`, `config_generate.py`, `config_sync.py`, `key_create.py`, and `key_limits.py`. It owns backend logic without `argparse` or CLI context. `commands/` contains only CLI adapters: metadata/options, conversion of arguments/context into explicit backend inputs, and CLI result/error handling. Create directories only for actual files, never empty placeholders. The role-based layout leaves CLI namespaces, aliases, options, preset-based component-layer selection, and the default output `build/llmproxy/litellm/config.gen.json` unchanged; custom `--config`/`--output` paths remain supported.

Component commands use `context.load_module(...)` with `src.config_generate`, `src.config_sync`, `src.key_create`, or `src.key_limits`. External callers use `llmproxy.core.component_modules.load_component_module(root, "llmproxy/litellm", module_name)`. The generic core owns loading/context, environment, HTTP/process, JSON merge, and resource utilities, not service-specific policy. There are no global LiteLLM library imports or compatibility shims.

Config sync `--dry-run` validates configuration resolution only, not a live diff or prune plan. It does not read live LiteLLM inventory or make management API changes, but resolution may query providers for model discovery.

## Local files

Management credentials and the former private Portainer dotenv assignments were consolidated into the root `.env` without printing values or overwriting existing root assignments. The retired Portainer dotenv path is removed after key-only verification. Private files use owner-scoped `*.local.*` names and mode `0600`.

Keep the private key override at `components/llmproxy/litellm/configs/key-limits.local.json`. Each contributing component owns its ignored `integrations/llmproxy/litellm/config.local.json`; neither root `.env` nor root presets move into LiteLLM. Local/private data and generated outputs are excluded from packages. When moving private files, preserve their contents and metadata without printing values; stop on destination collisions rather than overwriting them.

## Destination-namespaced contributions

For CLIProxyAPI and Headroom, public and local contributions move together into the source component's `integrations/llmproxy/litellm/` directory. Preserve file contents and private-file permissions when migrating; stop if a destination already exists rather than overwriting it. The historical flat filenames in the table above are no longer loaded and have no compatibility fallback.

LiteLLM still merges its base, all selected public contributions, then all selected local contributions. Preset exclusions continue to omit the source's contribution, and another target namespace is not consulted. Custom `--config` behavior, generated output location, Compose files, and live state are unchanged. No `component.toml` mapping is introduced.

## Test and packaging verification

LiteLLM's CLI, generator, sync, key-limit, and key-creation tests live under `components/llmproxy/litellm/tests/`. Root `tests/test_components.py` aggregates component tests into the normal root unittest discovery run; generic infrastructure tests remain under `tests/`.

```bash
taskset -c 0,1 uv run --with 'hatchling>=1.27,<2' --with editables python3 -m unittest discover -s tests -p 'test_*.py'
taskset -c 0,1 uv build
```

The `--with` packages provide ephemeral packaging/editable-install test dependencies, not new runtime dependencies. Public resource packaging includes stack files and component commands, libraries, schemas, and data under `components/`, plus root `presets/`.

## Runtime compatibility

The `all` public render retains the previous service, volume, network, route, config-reference, and stack topology. Current snapshot hashes are:

```text
monitoring     0a201cbee81da15d4e0c4b30bc5574a7dcf8298011bfaed0a64c500bef5e9183
llmproxy-data  ca146c25f981697f0d5447a86880b6d9a50a250d0b4bf5ab622ebecf3693a949
llmproxy       93e248ed0b849d1a60e0f9b8bb77630d94181b9a08e47aa53d19ef950af97cc9
```

Content-addressed config names remain stable when source bytes are unchanged.

Removing the empty monitoring base leaves Docker with a single Compose input. Docker then retains the top-level `x-common-healthcheck` extension (`interval: 30s`, `retries: 3`, `timeout: 10s`) that its multi-file merge previously dropped. This is the only monitoring render difference; the service healthcheck and all runtime resources are unchanged. The former monitoring hash was `3edec85e05ea1dbf5418214550e90be929eeb1ba7e582a506ada649a86c30990`. Thus `all` and `default` preserve monitoring runtime/topology parity, not byte parity. Every other stack's golden hash is unchanged.

The component-first `portainer` render is normalized-equivalent to the legacy Portainer manifest and preserves the `traefik`, `agent`, and `portainer` services, networks, volumes, host-mode ports, placement, and routing labels. Its preset is intentionally independent: neither `all` nor another existing production preset inherits it.

## Deployment boundary

No live deploy is part of this source refactor. The first live operation should target the compatibility preset `all`, retain the existing stack names, use a dry-run first, and verify exact remote state afterward. Moving to a smaller preset is a separate topology migration that may remove services and requires explicit authorization.

`default` is such a topology migration: it retains the complete `all` selection
except for the `llmproxy/headroom` component. Transitioning an existing `all` deployment
requires `--allow-remove-services`, which enables Docker `--prune`; the omitted
`headroom-data` volume is not automatically deleted.
