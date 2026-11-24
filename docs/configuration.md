# Configuration

## Operator environment

Copy `.env.example` to `.env`. One root `.env` owns deployment and LiteLLM management inputs. Selection controls which variables are required and which values reach child processes.

Keep `.env` mode `0600`.

## LiteLLM config pipeline

The owner is stack `llmproxy`, component `litellm`. Both config operations use the canonical prefix `uv run llmproxy llmproxy/litellm config`; the retired `component` prefix and old root `config` namespace have no compatibility aliases.

Inputs are merged in this order:

1. `components/llmproxy/litellm/configs/config.json`;
2. `integrations/llmproxy/litellm/config.json` from each component selected by the preset;
3. ignored `integrations/llmproxy/litellm/config.local.json` from each selected component.

All public contributions are merged in resolved preset order before any local contributions, which follow the same order. The source component owns each file; the destination `llmproxy/litellm` owns its schema and interpretation. No `component.toml` mapping is needed. For example, CLIProxyAPI contributes through `components/llmproxy/cli-proxy-api/integrations/llmproxy/litellm/config.json`, while its own runtime files remain under its `configs/` directory.

The base schema is `components/llmproxy/litellm/configs/config.schema.json`, beside `config.json`. `configs/` holds declarative inputs for either runtime or management: `configs/litellm.yaml` remains the runtime YAML, while `configs/config.json` is the management pipeline's base. These roles do not change their filenames or merge behavior.

The owning component's `src/` package contains `__init__.py`, `config_generate.py`, `config_sync.py`, `key_create.py`, and `key_limits.py`. Backend logic has no `argparse` or CLI context dependency. Thin `commands/` adapters load config backends via `context.load_module("src.config_generate")` and `context.load_module("src.config_sync")`; non-command callers use `llmproxy.core.component_modules.load_component_module(root, "llmproxy/litellm", module_name)`. There is no global LiteLLM backend package or compatibility shim. Create optional directories only when they contain real inputs or implementation.

An unselected component's local layer is never loaded. For `--config path/to/config.json`, only the custom base's sibling `config.local.json` is considered.

Client-visible model names have no interface prefix by default. At
`providers.<provider>.interfaces.<interface>.model_name_prefix`, omission, `null`,
or `""` leaves the name unchanged (for example, `o-0` or `a-0`). Set an explicit
string such as `"openai/"` to opt into prefixing. Model-level and per-name
`model_name_prefix` string overrides still take precedence. This affects only
`model_name`; backend `litellm_params.model` retains the interface prefix needed
to select the adapter, such as `openai/o-0` or `anthropic/a-0`.

When migrating from the previous interface-prefixed default, update routing
alias targets, fallback references, public-model-hub entries, and client model
selections or name-based access policies to match the new names. Explicit alias
keys are not renamed. Names previously separated by interface now share a model
group unless an explicit prefix is configured; backend targets in `model_aliases`
remain interface-qualified. Changing files does not synchronize live state.

`exclude_components` is applied before configuration-layer selection. Therefore
`default` does not load the Headroom public or local layer and does not generate
the `headroom-compression` guardrail.

Generate:

```bash
uv run llmproxy llmproxy/litellm config generate --preset default
```

Default output:

```text
build/llmproxy/litellm/config.gen.json
```

The file is ignored and written mode `0600`.

Use a custom base when required:

```bash
uv run llmproxy llmproxy/litellm config generate --config path/to/config.json --output build/llmproxy/litellm/custom.json
```

## Refresh Claude and GPT model configuration

`components/llmproxy/cli-proxy-api/scripts/refresh_model_config.py` refreshes
concrete model alias keys, `model_name_base_model_map` values, and `gpt-image-*`
model references in that component's `integrations/llmproxy/litellm/config.json`.
The default `--source public` reads official public Markdown catalogues and
**does not require an API key**:

- Claude: the `Claude API ID` row in `Latest models` at
  <https://platform.claude.com/docs/en/models/overview.md>.
- GPT text (`astra`, `sol`, `terra`, `luna`): canonical model links in
  `Featured models` at <https://developers.openai.com/api/docs/models.md>.
- GPT Image (`flare`, `sunburst`): the highest numeric canonical
  `gpt-image-VERSION-flare` / `-sunburst` model links in
  `Browse our full catalog of models` at
  <https://developers.openai.com/api/docs/models.md>.

For Claude and GPT text models, only the current-lineup sections are used (not
historical lists or prose mentions), and each required family must have one
unambiguous canonical ID. Missing sections/families, layout changes, ambiguous
versions, HTTP errors, or documents larger than 1 MiB abort without changing
the config; there is no silent API or stale-ID fallback. Public discovery
follows the published lineup, not the models available to a particular account,
and does not prove the gateway serves those models.

Run from the repository root (standard library and checkout only; no package
installation or credential environment required):

```bash
docker run --rm --cpus 1 --memory 256m \
  -e PYTHONDONTWRITEBYTECODE=1 \
  --mount "type=bind,src=$PWD,dst=/workspace" --workdir /workspace \
  python:3.11-slim-bookworm \
  python components/llmproxy/cli-proxy-api/scripts/refresh_model_config.py --provider all --dry-run
```

Replace `--dry-run` with `--write` to apply locally. Provider selection is
`--provider anthropic` (default), `openai`, or `all`; source selection defaults to
`public`. Use `--config PATH` for another source file. Preview is the default
when neither write-mode flag is supplied.

Concrete versioned alias keys are renamed, including `openai/claude-...` and
`anthropic/gpt-...`. The script also updates the matching Claude/GPT model IDs
already present as values of `model_name_base_model_map`, including fallback and
vision entries. Each value follows its own model family, even when its route
uses a different provider's protocol. For `gpt-image-*` lines (`flare`,
`sunburst`) under `--provider openai` or `all`, the script updates concrete
alias keys, alias targets (including wildcard targets such as `gpt-image-*` and
object alias `model` fields), `providers.*.interfaces.*.models` entries, and
matching `model_name_base_model_map` values together. Map keys, provider
prefixes, explicit per-model `base_model` overrides, text routing targets,
wildcard alias keys, and JSON formatting are preserved. Old concrete alias and
image model names are replaced, not retained as additional entries. Note that
updating `base_model` only updates LiteLLM pricing/capability metadata, whereas
updating `gpt-image-*` provider models and alias targets changes the backend
model ID requested from `cli-proxy-api` — verify gateway support before syncing.
Duplicate JSON keys and destination collisions abort. Discovery for all selected
providers completes before one atomic write; file changes detected during
discovery abort it. Writes use the repository's symlink-safe writer and produce
mode `0600`; already-current files are not rewritten. Local layers are not
merged or modified automatically; review any local alias overrides separately.

For optional account-specific discovery, select `--source api` explicitly and
pass only the selected provider's `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` through
the Docker environment (`-e VARIABLE_NAME`, never a literal key in argv).
Anthropic uses `/v1/models`, active `line` entries in newest-first order, and
pagination. OpenAI uses `/v1/models` and the highest numeric canonical
`gpt-VERSION-astra`, `-sol`, `-terra`, `-luna`, `gpt-image-VERSION-flare`, or
`gpt-image-VERSION-sunburst` version per required family; `created` is not a
release-order signal. This API-mode naming policy excludes snapshots, previews,
fine-tunes, and models whose shutdown date has arrived in UTC. The helper does
not read `.env`, CLI login files, or private configs for credentials; public
mode neither reads nor forwards API keys.

This helper does **not** generate resolved configuration, sync LiteLLM, deploy,
or change Portainer. Review the diff before the normal generate/sync flow.

## Sync

```bash
uv run llmproxy llmproxy/litellm config sync --preset default --dry-run
uv run llmproxy llmproxy/litellm config sync --preset default --only credentials,models
uv run llmproxy llmproxy/litellm config sync --preset default --only aliases,fallbacks,router_settings,general_settings,guardrails,access_groups
```

Supported sections are `credentials`, `models`, `aliases`, `fallbacks`, `public_model_hub`, `router_settings`, `general_settings`, `guardrails`, and `access_groups`.

Alias keys containing `*` are emitted as LiteLLM object aliases with `hidden: true`; their model target remains routable while the alias is omitted from discovery. Non-wildcard aliases retain their configured representation.

Top-level `general_settings` synchronizes individual LiteLLM `general_settings` fields via `POST /config/field/update` and verifies convergence via `GET /config/field/info`. Fields owned by `litellm.yaml` cannot be overwritten via API and fail preflight before any mutations. Both `router_settings` and `general_settings` merge only declared keys and do not prune undeclared server keys.

Top-level `guardrails` and `access_groups` follow the same three-state authority contract as `models` and `credentials`: omitted or `null` leaves live resources untouched even with `--prune`; an explicit empty `{}` or `[]` authorizes pruning stale live entries when `--prune` is enabled.

Top-level `access_groups` declares LiteLLM Unified Access Groups synchronized through `/v1/access_group`. In `access_model_names`, `$aliases:<pattern>` expands at generation time to all visible (non-hidden) aliases matching `<pattern>`, deduplicated in declaration order; unmatched `$aliases:` refs fail strict generation. Sync updates only the declared group fields and preserves live key and team memberships (`assigned_key_ids` / `assigned_team_ids`). With `--prune`, stale access groups are deleted only if all stale groups have zero assigned keys and zero assigned teams; any live assignment refuses the prune plan before the first delete.

`--dry-run` validates desired configuration resolution and selected-section identities, then stops before live LiteLLM inventory preflight or mutations. It does not compute a live diff, preview creates/updates/deletes or a prune plan, or verify management API connectivity. Configuration resolution may still query provider APIs for model discovery and requires complete provider inputs; dry-run is not an offline guarantee.

Invalid section names, missing files/environment, lookup failures, and partial API failures return non-zero. A failed operation is never followed by a success result.

Without `--force`, existing model identities retain their live configuration; differing desired parameters do not make a skipped model fail convergence. New models still require readback of the desired payload. Use `--force` to replace existing configurations. Credential prune with no stale names is a no-op and does not require readable secrets. When stale credentials exist, unsupported inventory evidence and projected model references are checked before synchronization writes; destructive readback checks remain in place.

Mutating sync requires complete provider credentials and trustworthy model/alias resolution. Missing API keys, missing resolved provider `interfaces`, malformed desired sections, missing inheritance or `$base` targets, unmatched `$models:` aliases, alias cycles, unresolved routing references, or incomplete provider discovery abort before synchronization writes. Providers may inherit `interfaces`; an explicitly empty resolved interface map is intentional empty state, unlike an omitted field. Omitted credentials, models, aliases, fallbacks, or public-model-hub sections mean “leave unchanged”, including during `--prune`; only an explicitly supplied empty section clears or prunes that state. Alias-to-public-hub expansion is opt-in through `public_model_hub_aliases_autofill_enabled`; an explicit `public_model_hub` list always wins. A generated config can be supplied again through `--config` without losing resolved sections or gaining destructive authority. `router_settings` cannot contain the dedicated alias or fallback keys.

Routing preflight combines selected desired updates with preserved live routing settings. When models are unmanaged, it reads live model inventory rather than treating omission as an empty model list. It validates alias resolution and every fallback class before the first mutation and again when planning model deletion. Ordinary `fallbacks` use their dedicated section; `context_window_fallbacks`, `content_policy_fallbacks`, and `default_fallbacks` are managed through `router_settings`. A wildcard `*` source is supported; fallback targets must resolve to a model, directly or through valid aliases. Removing an alias or model cannot leave a retained fallback dangling: explicitly retarget or clear the affected fallback in the selected desired state too. Unselected clearing instructions do not authorize removing those references.

Synchronization runs strictly sequentially as one write → immediate item readback verification → next write, followed by optional model prune and optional credential prune. Credential writes attach an HMAC-based opaque configuration fingerprint in `credential_info`. Non-pruning credential readback and models-only dependency checks require a matching fingerprint and desired metadata, plus matching plaintext values or the exact LiteLLM default mask for sensitive fields (first two characters + four asterisks + last two characters; strings of at most four characters become five asterisks). Missing fields, mismatched visible characters, unknown mask formats, and mismatched non-sensitive values fail closed. This permits masked readback without a per-sync warning; it checks the returned projection and marker, not equality of the hidden secret bytes. Credential prune additionally requires matching readable values and keyed fingerprints for every inventory entry before authorizing any delete; name-only or mixed-evidence inventories are never destructive authority. A models-only sync reads and verifies every referenced live credential before the first model write. A failed write, inventory read, or convergence check blocks every later mutation. Model prune reads aliases, fallbacks, and the public model hub and validates the complete deletion plan before the first delete; delete loops stop on the first failure and final inventory convergence is required. Stale credentials referenced by live models are never pruned. Replacement creates and reads back a distinct correct model ID while proving the full pre-write model inventory remains intact, before deleting older IDs; it then verifies every old ID is absent. Management HTTP requests use a bounded 30-second timeout, dynamic endpoint segments and provider pagination cursors are encoded, and default HTTP error messages omit response bodies to avoid leaking credentials.

## API key limits

Tracked defaults and rules:

```text
components/llmproxy/litellm/configs/key-limits.json
components/llmproxy/litellm/configs/key-limits.schema.json
```

Ignored private overrides:

```text
components/llmproxy/litellm/configs/key-limits.local.json
```

Key creation and key-limit backend modules are `src.key_create` and `src.key_limits`, loaded through the same generic loader. Canonical key commands use `llmproxy llmproxy/litellm create-key` and `llmproxy llmproxy/litellm key-limits`. Existing global `key create` and `key limits` aliases remain supported.

Key-limit commands apply immediately by default. Use `--dry-run` to list keys and preview changes without updating them. There are no `--apply` or `--yes` flags for key limits.

```bash
uv run llmproxy llmproxy/litellm key-limits --max-budget 700 --dry-run
uv run llmproxy llmproxy/litellm key-limits --max-budget 700
```

These commands also set the default RPM limit to `100` and budget duration to `7d`; they do not reset spend unless `--reset-spend` is supplied. Per-key rules can override the budget/RPM defaults, and keys matching `default_user_id` remain excluded.
