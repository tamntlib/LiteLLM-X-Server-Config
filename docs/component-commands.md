# Component commands

Component-specific commands live at:

```text
components/<stack>/<component>/commands/*.py
```

A command module is a thin CLI adapter: it declares metadata/options, translates arguments and context into backend inputs, and maps results/errors to CLI output and status. Backend policy belongs in the component's `src/`, without `argparse` or CLI context. The minimal command contract is:

```python
COMMAND = "health"
DESCRIPTION = "Check component health"
MUTATING = False
REQUIRES_CONFIRMATION = False
ALIASES = (("health", "litellm"),)  # optional


def configure(parser):
    parser.add_argument("--timeout", type=int, default=10)


def run(args, context):
    return 0
```

The metadata constants must be Python literals so discovery can read them with `ast.literal_eval` without importing the module. The canonical form is `llmproxy <stack>/<component> <command> [<subcommand> ...]`. The full identifier avoids collisions when different stacks contain components with the same name. For the hypothetical `health` adapter above, the invocation would be:

```bash
uv run llmproxy llmproxy/litellm health
```

Declared aliases are added only if no other command owns the same path. The retired `component` prefix has no compatibility alias. Existing global `key create` and `key limits` aliases remain available.

`COMMAND` may also be a tuple of path segments for a nested command. The `litellm` component in stack `llmproxy` owns these config command modules:

| File | Literal metadata |
|---|---|
| `components/llmproxy/litellm/commands/config_generate.py` | `COMMAND = ("config", "generate")` |
| `components/llmproxy/litellm/commands/config_sync.py` | `COMMAND = ("config", "sync")` |

These commands declare no global aliases. The former root `config` namespace is removed, not retained as a compatibility shim. The generator, sync backend, data, schemas, and tests are also LiteLLM-owned, not just the command wrappers.

`context` contains the resource root, component identifier, and component directory. Commands should resolve owned files relative to `context.component_dir` or `context.root`, not the caller's current directory.

## Component libraries

Load an owned backend through the context instead of a global service-specific import. This example illustrates module loading only; call the backend with explicit inputs in the adapter, not by passing the CLI namespace or context into `src/`:

```python
def load_config_backend(context):
    return context.load_module("src.config_generate")
```

Callers outside a component command use the generic loader explicitly:

```python
from llmproxy.core.component_modules import load_component_module


def load_litellm_backends(root):
    module_names = (
        "src.config_generate",
        "src.config_sync",
        "src.key_create",
        "src.key_limits",
    )
    return {
        name: load_component_module(root, "llmproxy/litellm", name)
        for name in module_names
    }
```

These names resolve `src/config_generate.py`, `src/config_sync.py`, `src/key_create.py`, and `src/key_limits.py` under `components/llmproxy/litellm/`, including when that component comes from packaged public resources. The `src/` package contains `__init__.py`. Declarative runtime YAML, management config JSON, key rules, and adjacent JSON schemas belong in `configs/`, not Python modules or CLI adapters. Create optional directories only when they have real contents. Do not create global service-specific packages, compatibility shims, or `sys.path` edits. Generic context, HTTP, environment, and resource helpers remain importable from `llmproxy.core`.

Colocate backend tests in `components/<stack>/<component>/tests/test_*.py`. Root `tests/test_components.py` includes them in the normal unittest discovery run; see [Architecture](architecture.md#test-ownership-and-discovery) for the packaging-test dependency command.

## Safety

- Deploy never executes component command files.
- A mutating operation must set `MUTATING = True`.
- Operations requiring an additional destructive confirmation set `REQUIRES_CONFIRMATION = True`; the CLI adds and enforces `--yes` before calling `run`.
- Network, API, and lookup failures must fail closed.
- Return `0` on success; raise `CommandError` for concise CLI errors and exit code 2.

Current LiteLLM-owned commands:

```bash
uv run llmproxy llmproxy/litellm config generate --help
uv run llmproxy llmproxy/litellm config sync --help
uv run llmproxy llmproxy/litellm create-key --help
uv run llmproxy llmproxy/litellm key-limits --help
```

Unique aliases:

```bash
uv run llmproxy key create --help
uv run llmproxy key limits --help
```
