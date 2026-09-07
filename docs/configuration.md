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

## Sync

```bash
uv run llmproxy llmproxy/litellm config sync --preset default --dry-run
uv run llmproxy llmproxy/litellm config sync --preset default --only credentials,models
uv run llmproxy llmproxy/litellm config sync --preset default --only aliases,fallbacks,router_settings,guardrails
```

Supported sections are `credentials`, `models`, `aliases`, `fallbacks`, `public_model_hub`, `router_settings`, and `guardrails`.

`--dry-run` validates desired configuration resolution and selected-section identities, then stops before live LiteLLM inventory preflight or mutations. It does not compute a live diff, preview creates/updates/deletes or a prune plan, or verify management API connectivity. Configuration resolution may still query provider APIs for model discovery and requires complete provider inputs; dry-run is not an offline guarantee.

Invalid section names, missing files/environment, lookup failures, and partial API failures return non-zero. A failed operation is never followed by a success result.

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

Key creation and key-limit backend modules are `src.key_create` and `src.key_limits`, loaded through the same generic loader. Canonical key commands use `llmproxy llmproxy/litellm create-key` and `llmproxy llmproxy/litellm key-limits`. Existing global `key create` and `key limits` aliases and command options remain unchanged.

Dry-run and apply:

```bash
uv run llmproxy key limits --dry-run
uv run llmproxy key limits --apply
```
