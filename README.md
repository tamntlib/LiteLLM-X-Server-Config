# LiteLLM-X-Server-Config

A self-hosted LLM proxy stack built around [LiteLLM](https://github.com/BerriAI/litellm), [Headroom](https://github.com/headroomlabs-ai/headroom), [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), PostgreSQL, Netdata, and Traefik. It provides centralized API key management, model routing, global prompt compression, access-group control, Claude Code request validation, and optional monitoring for a Docker Swarm deployment managed through Portainer. TLS is terminated by Traefik using Let's Encrypt certificates obtained via Cloudflare DNS challenge, with Cloudflare Proxy (orange cloud) providing CDN and DDoS protection.

## Architecture

This repository is deployed as multiple Docker Swarm stacks:

### Infrastructure stack (`portainer/portainer.yaml`)

- `traefik`: Shared ingress proxy with Let's Encrypt TLS via Cloudflare DNS challenge
- `portainer`: Docker Swarm management UI
- `agent`: Portainer agent for Swarm node access

### Application data stack (`llmproxy-data.yaml`)

- `db`: PostgreSQL database for LiteLLM state, usage logs, and model configuration

### Application stack (`llmproxy.yaml`)

- `cli-proxy-api`: Anthropic-compatible proxy and auth service
- `cli-proxy-api-usage`: Usage keeper dashboard for CLIProxyAPI
- `headroom`: Prompt-compression proxy called by LiteLLM, with its dashboard exposed through Traefik BasicAuth
- `litellm`: Core routing layer and LiteLLM admin UI

### Monitoring stack (`monitoring/netdata.yaml`)

- `netdata`: Host, container, and PostgreSQL monitoring dashboard

### Networks

- `internal`: private overlay network between application services and PostgreSQL
- `public`: shared overlay network for Traefik and routed services
- `monitoring`: shared overlay network used by Netdata auto-discovery

## Routing

Traffic flows through Cloudflare Proxy (orange-cloud DNS records) to Traefik, which terminates TLS using Let's Encrypt certificates obtained via Cloudflare DNS challenge. Traefik trusts Cloudflare's IP ranges for `X-Forwarded-For` headers and redirects HTTP to HTTPS.

Set your Cloudflare SSL/TLS mode to **Full (Strict)** so that Cloudflare verifies the Let's Encrypt certificate on your origin.

### Shared routing variables

Use the same routing variables in [portainer/.env.example](/Users/tamnt/My-Projects/LiteLLM-X-Server-Config/portainer/.env.example), [monitoring/.env.example](/Users/tamnt/My-Projects/LiteLLM-X-Server-Config/monitoring/.env.example), and [.env.example](/Users/tamnt/My-Projects/LiteLLM-X-Server-Config/.env.example):

| Variable | Default | Description |
|------|------|-------------|
| `TRAEFIK_ROUTER_ENTRYPOINTS` | `websecure` | Traefik entrypoint for routed services |
| `TRAEFIK_ROUTER_TLS` | `true` | Enable TLS on Traefik routers |
| `LETSENCRYPT_RESOLVER` | `le` | Traefik certificate resolver name |

## Prerequisites

```sh
# Install ptctools
uv tool install ptctools --from git+https://github.com/tamntlib/ptctools.git
```

## Installation

### 1. Install Portainer CE and Traefik

#### Configure DNS

Create DNS `A`/`AAAA` records in Cloudflare for your hostnames (`portainer.example.com`, `netdata.example.com`, `llm.example.com`, `headroom.llm.example.com`, `cli-proxy-api.llm.example.com`) pointing to your server's public IP. Enable the orange-cloud proxy for each record.

Set your Cloudflare domain's SSL/TLS mode to **Full (Strict)**.

#### Create a Cloudflare API token

Create an API token at <https://dash.cloudflare.com/profile/api-tokens> with **Zone → DNS → Edit** permission. This is used by Traefik to solve Let's Encrypt DNS challenges.

#### Create the Portainer config directory on the server

```sh
ssh root@<ip> 'mkdir -p /opt/portainer'
```

#### Copy the Portainer stack files to the server

```sh
scp portainer/portainer.yaml root@<ip>:/opt/portainer/
scp portainer/.env.example root@<ip>:/opt/portainer/.env
```

#### SSH to server

##### Install Docker

<https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository>

##### Configure the Portainer stack

```sh
docker swarm init
```

Edit `/opt/portainer/.env` and set at least:

- `PORTAINER_HOST`
- `CF_DNS_API_TOKEN=<token>` — Cloudflare API token for Let's Encrypt DNS challenge
- `LETSENCRYPT_EMAIL=<email>` — email address for Let's Encrypt

##### Deploy Portainer

```sh
docker stack deploy -c /opt/portainer/portainer.yaml portainer
```

### 2. Deploy the monitoring stack

Deploy this first so the shared `monitoring` overlay network exists before the application stacks join it.

#### Expose the Netdata hostname

Add a DNS `A`/`AAAA` record for `netdata.example.com` in Cloudflare pointing to your server IP, with the orange-cloud proxy enabled.

#### Set environment variables

Copy `monitoring/.env.example` to `monitoring/.env` and fill in the values:

```sh
cp monitoring/.env.example monitoring/.env
```

Required environment variables:

- `NETDATA_HOST`: Hostname for the Netdata dashboard
- `NETDATA_BASIC_AUTH`: Basic auth credentials for Traefik

#### Create configs and deploy

```sh
ptctools docker config set -n monitoring_netdata-conf -f 'monitoring/configs/netdata.conf'
ptctools docker stack deploy -n monitoring -f 'monitoring/netdata.yaml' --ownership team
```

### 3. Deploy the application stacks from your local machine

#### Expose the application hostnames

Add DNS `A`/`AAAA` records for `llm.example.com`, `headroom.llm.example.com`, `cli-proxy-api.llm.example.com`, and `cli-proxy-api-usage.llm.example.com` in Cloudflare pointing to your server IP, with the orange-cloud proxy enabled.

#### Set environment variables

Copy `.env.example` to `.env` and fill in the values:

```sh
cp .env.example .env
```

Required environment variables:

- `DB_USER`, `DB_PASSWORD`, `DB_NAME`, `DB_HOST`: PostgreSQL credentials and host
- `LITELLM_HOST`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`: LiteLLM configuration
- `HEADROOM_HOST`, `HEADROOM_API_KEY`, `HEADROOM_BASIC_AUTH`: Headroom dashboard hostname, shared proxy token, and Traefik BasicAuth credentials
- `CLI_PROXY_API_HOST`: CLIProxyAPI hostname
- `CLI_PROXY_API_USAGE_HOST`, `CLI_PROXY_API_USAGE_LOGIN_PASSWORD`: CLIProxyAPI usage dashboard configuration

Optional environment variables used by the stack:

- `CLAUDE_CODE_MODELS`: Comma-separated model names that should enforce Claude Code checks
- `CLAUDE_CODE_MIN_VERSION`: Minimum allowed Claude Code version for those models
- `HEADROOM_TELEMETRY`: Enable Headroom's local event recording and dashboard statistics; defaults to `on`
- `HEADROOM_COMPRESS_USER_MESSAGES`: Set to `1` to compress user/system messages, which is required for most Claude Code traffic
- `HEADROOM_COMPRESS_ALLOW_REMOTE`: Set to `1` so LiteLLM can call `/v1/compress` from its separate container
- `SLACK_WEBHOOK_URL`: LiteLLM Slack webhook

#### Optional local-only stack overlay

Files whose names match `*.local*` are gitignored throughout the repository. Use this convention for deployment-specific configuration that must not be committed, for example:

- `llmproxy.local.yaml`: local Docker Swarm stack additions or overrides
- `configs/litellm.local.yaml`: local config content uploaded as an external Docker config

For example, create `llmproxy.local.yaml` to mount an additional external config into LiteLLM:

```yaml
services:
  litellm:
    configs:
      - source: litellm-local-config-yaml
        target: /app/litellm.local.yaml

configs:
  litellm-local-config-yaml:
    name: ${LITELLM_LOCAL_CONFIG_NAME:-llmproxy_litellm-local-config-yaml}
    external: true
```

Create or update the external config from the local content file:

```sh
ptctools docker config set \
  -n "${LITELLM_LOCAL_CONFIG_NAME:-llmproxy_litellm-local-config-yaml}" \
  -f 'configs/litellm.local.yaml' \
  --ownership team
```

If you override `LITELLM_LOCAL_CONFIG_NAME`, export it in the shell before running `config set` and keep the same value in the stack environment. Use `--force` only when replacing an existing Docker config. Then merge the public stack with the local overlay:

```sh
docker stack config --skip-interpolation \
  -c 'llmproxy.yaml' \
  -c 'llmproxy.local.yaml' \
  > 'llmproxy.gen.yaml'
```

The base file must come first and the local overlay second. `--skip-interpolation` keeps `${...}` placeholders in the generated file instead of writing resolved environment values into it. Both `llmproxy.local.yaml` and `llmproxy.gen.yaml` are gitignored.

Deploy the generated stack file:

```sh
ptctools docker stack deploy \
  -n llmproxy \
  -f 'llmproxy.gen.yaml' \
  --ownership team
```

Because `llmproxy.gen.yaml` remains in the repository root, `ptctools` loads the adjacent `.env` file as usual. Regenerate it whenever either `llmproxy.yaml` or `llmproxy.local.yaml` changes.

#### Upload configs and deploy

The commands below deploy the standard stack without a local overlay. If you use the optional overlay flow above, upload the public configs and deploy `llmproxy-data` as shown, then deploy `llmproxy.gen.yaml` instead of `llmproxy.yaml`.

```sh
export PORTAINER_URL=https://portainer.example.com
export PORTAINER_ACCESS_TOKEN=<token>

ptctools docker config set -n llmproxy_litellm-config-yaml -f 'configs/litellm.yaml' --ownership team
ptctools docker config set -n llmproxy_cli-proxy-api-config-yaml -f 'configs/cli-proxy-api.yaml' --ownership team

ptctools docker stack deploy -n llmproxy-data -f 'llmproxy-data.yaml' --ownership team
ptctools docker stack deploy -n llmproxy -f 'llmproxy.yaml' --ownership team
```

## LiteLLM management

```sh
cd litellm_scripts

# Generate a resolved config from config.json + config.local.json
uv run python gen_config.py

# Full sync including globally enabled guardrails
uv run python config.py --only credentials,models,aliases,fallbacks,public_model_hub,router_settings,guardrails --force --prune

# Sync specific components
uv run python config.py --only models --force
uv run python config.py --only aliases,fallbacks,public_model_hub
uv run python config.py --only public_model_hub
uv run python config.py --only guardrails --force

# Create a LiteLLM user and API key
uv run python create_api_key.py user@example.com
uv run python create_api_key.py user@example.com --alias my-key

# Preview and apply limits for existing API keys
uv run python update_api_key_limits.py
uv run python update_api_key_limits.py --yes
```

Required environment variables in `litellm_scripts/.env`:

- `LITELLM_API_KEY`
- `LITELLM_BASE_URL`

## Configuration files

| File | Description |
|------|-------------|
| `portainer/portainer.yaml` | Infrastructure stack with Traefik (Let's Encrypt + Cloudflare DNS challenge) and Portainer |
| `portainer/.env` | Environment variables for the Portainer/Traefik stack |
| `llmproxy-data.yaml` | PostgreSQL Docker Swarm stack |
| `llmproxy.yaml` | Application Docker Swarm stack for LiteLLM, Headroom, and CLIProxyAPI |
| `monitoring/netdata.yaml` | Monitoring stack with Netdata and the label-watching config generator |
| `configs/litellm.yaml` | LiteLLM runtime config (callbacks, DB batching, connection pool settings) |
| `configs/cli-proxy-api.yaml` | CLIProxyAPI runtime config |
| `litellm_scripts/config.json` | Base provider/model/alias/fallback/public-model-hub/router/guardrail config |
| `litellm_scripts/config.local.json` | Local overrides including API keys (gitignored, deep-merged with `config.json`) |
| `litellm_scripts/config.gen.json` | Generated resolved config output from `gen_config.py` with LiteLLM-ready credential and model request bodies |
| `litellm_scripts/api_key_limits.json` | Shared per-key RPM and max-budget overrides |
| `litellm_scripts/api_key_limits.local.json` | Private per-key limit overrides (gitignored, deep-merged with `api_key_limits.json`) |
| `.env` | Environment variables for the application stacks |
| `monitoring/.env` | Environment variables for the monitoring stack |

### Local configuration (`config.local.json`)

Create `litellm_scripts/config.local.json` to add API keys and local overrides:

```json
{
  "providers": {
    "my-provider": {
      "api_key": "sk-your-api-key-here"
    },
    "another-provider": {
      "api_key": "sk-another-key"
    }
  }
}
```

This file is deep-merged with `config.json`, so you only need to specify overrides. Provider configs can also use `$extend` in `config.json` and override or disable inheritance in `config.local.json`.

### API key limit overrides

Add shared overrides to `litellm_scripts/api_key_limits.json` and private overrides to the gitignored `litellm_scripts/api_key_limits.local.json`:

```json
{
  "key-alias": {
    "rpm_limit": 4000,
    "max_budget": 5000
  }
}
```

Selectors can match a key alias, key name, hash/token, API key, user ID, or email. The local file is deep-merged over the base file, including individual fields for the same selector. If a key matches multiple selectors, the first selector wins. Keys or fields without an override use the script/CLI defaults: 100 RPM and a 3000 max budget by default.

Run `uv run python update_api_key_limits.py` to inspect the effective limits without changing keys, then add `--yes` only after reviewing the dry-run output. Raw keys and other sensitive selectors should only be stored in `api_key_limits.local.json`.

### Global Headroom compression

The application stack pulls the public `ghcr.io/headroomlabs-ai/headroom` image. The `headroom-compression` guardrail sets `api_base` directly to `http://headroom:8787`, so LiteLLM calls `/v1/compress` over the private `internal` network without a separate `HEADROOM_API_BASE` environment variable. This container-to-container request is not loopback, so `HEADROOM_COMPRESS_ALLOW_REMOTE=1` is required.

Put only the raw Headroom token override in the gitignored `litellm_scripts/config.local.json`. Guardrails are keyed by name and deep-merged with the base config:

```json
{
  "guardrails": {
    "headroom-compression": {
      "litellm_params": {
        "api_key": "your-raw-headroom-token"
      }
    }
  }
}
```

`gen_config.py` converts the keyed object into LiteLLM's required list format and adds `guardrail_name` from the object key. `config.py` then sends the merged guardrail to LiteLLM's Guardrail API. The token is stored in LiteLLM's guardrail database configuration; neither `config.py` nor the LiteLLM container reads `HEADROOM_API_KEY` from the root `.env`. The root `.env` still needs the same token for the Headroom container and Traefik header middleware.

The dashboard is available at `https://${HEADROOM_HOST}/dashboard` through this request flow:

```text
Browser
  -> Traefik BasicAuth
  -> inject X-Headroom-Proxy-Token
  -> Headroom dashboard
```

Generate `HEADROOM_BASIC_AUTH` with `htpasswd` and escape every `$` as `$$` before putting it in `.env`:

```sh
htpasswd -nb admin '<password>' | sed -e 's/\$/\$\$/g'
```

Traefik only publishes `/dashboard`, `/stats*`, `/health`, and `/favicon.ico`; `/v1/compress` remains unavailable through the public router. BasicAuth removes the browser's `Authorization` header, then the second middleware injects `X-Headroom-Proxy-Token` from `HEADROOM_API_KEY`. The resolved raw proxy token is intentionally visible in the service's Docker/Portainer labels and stored in LiteLLM's guardrail database configuration, so access to both systems must remain restricted.

`HEADROOM_TELEMETRY=on` enables Headroom's local event recording used by dashboard statistics; in the current Headroom image this does not upload request data to Headroom Labs. Some dashboard panels, including the transformations feed and settings, may remain unavailable remotely because Headroom itself restricts their backing endpoints to loopback callers. Aggregate statistics, history, and health remain available.

The `headroom-compression` guardrail in `litellm_scripts/config.json` uses `default_on: true`, so it applies to every virtual key and model without requiring a per-request `guardrails` field. Register or reconcile it in LiteLLM's database after deploying the stack:

```sh
cd litellm_scripts
uv run python config.py --only guardrails --force
```

LiteLLM Headroom integration requires LiteLLM v1.92.x or newer. The LiteLLM image remains unpinned by design, so verify the running version after each deployment. Confirm compression by checking the `x-litellm-applied-guardrails: headroom-compression` response header or the Guardrails panel in LiteLLM Logs. Anthropic messages carrying `cache_control` markers are intentionally not compressed.

### Provider-level `default_model`

Use `default_model` to define model configuration that should be deep-merged into every explicit or auto-discovered model under a provider:

```json
{
  "providers": {
    "my-provider": {
      "default_model": {
        "model_info": {
          "max_input_tokens": 272000
        }
      },
      "models": {
        "model-a": {},
        "model-b": {
          "model_info": {
            "max_input_tokens": 128000
          }
        }
      },
      "interfaces": {
        "openai": {}
      }
    }
  }
}
```

`model-a` inherits `max_input_tokens: 272000`; `model-b` overrides it with `128000`. Nested `model_info` and `litellm_params` objects are deep-merged. Precedence, from lowest to highest, is:

1. provider-level `default_model`
2. provider-level `models.<model>`
3. interface-level `interfaces.<interface>.models.<model>`
4. per-name overrides in the object form of `model_names`

To remove inherited keys, add a reserved `$delete` array in the same object. For example, `"model_info": {"$delete": ["max_input_tokens"]}` removes `max_input_tokens`; multiple keys can be listed in the same array. This deletion syntax applies recursively to every configuration merged by `deep_merge`, not only model configuration, and `$delete` is not emitted in the generated output.

Generated model aliases inherit the resolved defaults from their target model. Top-level manual `models` entries are not provider models and do not inherit `default_model`.

### Interface-level `api_base`

Each interface may override the provider-level `api_base`. This is useful when a single provider exposes different OpenAI-compatible and Anthropic-compatible endpoints.

```json
{
  "providers": {
    "my-provider": {
      "api_base": "https://shared-gateway.example.com",
      "interfaces": {
        "anthropic": {
          "api_base": "https://custom-anthropic.example.com"
        },
        "openai": {
          "api_base": "https://custom-openai.example.com/v1"
        }
      }
    }
  }
}
```

Rules:

- interface-level `api_base` overrides the provider-level `api_base` for credential generation and interface-specific model discovery
- interface-level `models_api_base` may be set separately when the `/models` endpoint lives on a different base URL
- if interface `models_api_base` is omitted, model discovery falls back to interface `api_base`, then provider-level `models_api_base`, then provider-level `api_base`

### `public_model_hub` and `is_public_model_hub`

Use `public_model_hub` to add explicit model groups or aliases to LiteLLM's public model hub:

```json
{
  "public_model_hub": [
    "claude-opus-4-7"
  ]
}
```

Use `is_public_model_hub` to derive public model hub entries from config defaults:

```json
{
  "providers": {
    "my-provider": {
      "is_public_model_hub": true,
      "interfaces": {
        "openai": {
          "models": {
            "model-a": null,
            "model-b": {
              "is_public_model_hub": false
            }
          }
        }
      }
    }
  }
}
```

Rules:

- provider-level `is_public_model_hub` is the default for all models under that provider
- model-level `is_public_model_hub` overrides the provider default
- if `is_public_model_hub` is omitted, it is treated as `false`
- `public_model_hub` entries are combined from three sources by default: derived model entries, alias names, and the explicit `public_model_hub` array
- set `public_model_hub_autofill_disabled: true` to disable derived model entry autofill
- set `public_model_hub_aliases_autofill_disabled: true` to disable alias-name autofill
- in `config.local.json`, the `public_model_hub` array replaces the base list instead of merging element-by-element

### `model_name_prefix`

Each interface may define `model_name_prefix` to control derived model group names.

`model_name_prefix` is treated as a literal prefix string:

- omitted, `null`, or `""` → no prefix, so the model name is just the model ID
- `"openai"` → `openaimodel`
- `"openai/"` → `openai/model`
- `"openai-"` → `openai-model`

Examples:

- `interfaces.anthropic.models.claude-sonnet-4-6` resolves to `claude-sonnet-4-6`
- `interfaces.openai.models.gpt-5.4` resolves to `gpt-5.4`
- `interfaces.gemini.models.gemini-2.5-pro` with `"model_name_prefix": "gemini-"` resolves to `gemini-gemini-2.5-pro`

```json
{
  "providers": {
    "my-provider": {
      "interfaces": {
        "anthropic": {
          "model_name_prefix": "anthropic/",
          "models": {
            "claude-sonnet-4-6": null
          }
        }
      }
    }
  }
}
```

With no explicit `model_name`, the generated model group name becomes `<model_name_prefix><model-id>`. In the example above, `claude-sonnet-4-6` resolves to `anthropic/claude-sonnet-4-6`.

If `model_name` is set on a model, it still wins and fully overrides the derived prefix-based name.

These resolved prefixed names are the ones used by generated models and should be the names you reference in:

- `aliases` targets
- `fallbacks`
- `public_model_hub`
- `model_name_base_model_map` entries when you want to key by resolved model name instead of raw provider model ID

### Model-level `access_groups`

Individual models can override the provider-level `access_groups` by specifying `access_groups` in their model config. You can also override `access_groups` per generated model name by using the object form of `model_names` with the reserved key `$self`:

```json
{
  "providers": {
    "my-provider": {
      "access_groups": ["General"],
      "models": {
        "model-a": null,
        "model-b": {
          "access_groups": ["Premium"]
        },
        "model-c": {
          "access_groups": ["Internal"],
          "model_names": {
            "$self": {},
            "alias": {
              "access_groups": ["General"]
            }
          }
        }
      }
    }
  }
}
```

- `model-a` inherits the provider-level `access_groups`: `["General"]`
- `model-b` uses its own `access_groups`: `["Premium"]`
- `model-c` generates two entries: `model-c` uses `["Internal"]` and `alias` uses `["General"]`

```json
{
  "providers": {
    "my-provider": {
      "access_groups": ["General"],
      "models": {
        "model-a": null,
        "model-b": {
          "access_groups": ["Premium"]
        }
      }
    }
  }
}
```

- `model-a` inherits the provider-level `access_groups`: `["General"]`
- `model-b` uses its own `access_groups`: `["Premium"]`

## Backup and restore

```sh
# Volume backup/restore (uses Duplicati)
ptctools docker volume backup -v vol1,vol2 -o s3://mybucket
ptctools docker volume restore -i s3://mybucket/vol1
ptctools docker volume restore -v vol1 -i s3://mybucket/vol1

# Database backup/restore (uses minio/mc for S3)
ptctools docker db backup -c container_id -v db_data \
  --db-user postgres --db-name mydb -o backup.sql.gz
ptctools docker db backup -c container_id -v db_data \
  --db-user postgres --db-name mydb -o s3://mybucket/backups/db.sql.gz

ptctools docker db restore -c container_id -v db_data \
  --db-user postgres --db-name mydb -i backup.sql.gz
ptctools docker db restore -c container_id -v db_data \
  --db-user postgres --db-name mydb -i s3://mybucket/backups/db.sql.gz
```

## Monitoring

Netdata collects host, container, and PostgreSQL metrics.

### Metrics retention

Netdata limits local metrics storage to 10 GiB in `monitoring/configs/netdata.conf`, which provides roughly 2-4 weeks of retention depending on metric volume.

### PostgreSQL auto-discovery

Netdata uses its built-in Docker auto-discovery to collect PostgreSQL metrics from containers that expose port `5432` or use a PostgreSQL image. To allow the default discovered job to connect, create a dedicated monitoring user in each PostgreSQL container:

```sh
psql -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" -c "CREATE USER netdata WITH PASSWORD 'postgres'; GRANT pg_monitor TO netdata;"
```

The service must join the shared `monitoring` network so the Netdata stack can reach the discovered container address.
