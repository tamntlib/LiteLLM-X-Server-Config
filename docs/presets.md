# Presets

Presets are TOML files under `presets/`. They select components explicitly and may inherit from one parent.

`default` is the full topology without Headroom. Its name does not enable automatic selection: commands still require an explicit `--preset default` where a preset is required.

```toml
description = "PostgreSQL and LiteLLM without public ingress"
components = [
  "llmproxy-data/postgres",
  "llmproxy/litellm",
]
features = []
```

Inheritance appends unique components and features while preserving stable order:

```toml
description = "LiteLLM through Traefik"
extends = "litellm-only"
features = ["traefik"]
```

A derived preset may subtract components selected by its inheritance chain:

```toml
description = "Current full topology without Headroom compression"
extends = "all"
exclude_components = ["llmproxy/headroom"]
```

Exclusions are component-scoped rather than post-render service filters. This also removes
the component's overlays, local overrides, required inputs, Docker configs, and LiteLLM
configuration layers. Excluding a component that is not selected, or both adding and
excluding the same component in one preset, is an error. Dependency validation runs after
exclusions, so a remaining component cannot silently lose a required dependency.

Resolution is level-by-level. At each level, inherited exclusions are already applied,
then that level's `components` are appended in declaration order, and finally that level's
`exclude_components` are removed in declaration order. Therefore a child may explicitly
re-add a component excluded by its parent; it is appended at the child's position. A preset
may not add and exclude the same component at one level, repeat an exclusion, or exclude a
component that is not currently selected.

This mechanism does not remove an arbitrary individual service from a component that owns
multiple services. Excluding a one-service component naturally omits that service;
excluding a multi-service component omits every service and resource it owns.

A component directory appearing on disk does not add it to any preset. This prevents experimental components from entering `all` automatically.

The `portainer` preset selects only `portainer/portainer` (Agent + Portainer). The independent `traefik` preset selects only `traefik/traefik` (ingress). Neither is inherited by `all` or the LiteLLM preset hierarchy. Application presets' `traefik` feature enables routing overlays, not deployment of the ingress stack. Every ingress consumer, including Traefik and Portainer, requires the independent external `public` overlay to be created by Docker CLI first. Traefik also requires its certificate volume/node prerequisite. No preset creates `public` and no network-owner ordering dependency exists between the management and ingress components. Stack updates reuse the existing external network; presets do not recreate or change ownership of shared infrastructure.

## Current hierarchy

```text
litellm-only
├── litellm-traefik
│   └── llmproxy
│       └── all
│           └── default
└── litellm-standalone

portainer
traefik
```

List and inspect:

```bash
uv run llmproxy presets
uv run llmproxy inputs --preset default
uv run llmproxy inputs --preset portainer
uv run llmproxy inputs --preset traefik
```

Dependency validation ensures every component named by `requires` is present in the resolved preset. Cycles, unknown presets, and unknown components fail before rendering or deployment.
