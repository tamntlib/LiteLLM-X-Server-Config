# LLM Proxy

A self-hosted LLM proxy stack built around [LiteLLM](https://github.com/BerriAI/litellm), [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI), PostgreSQL, and Netdata. It provides centralized API key management, model routing, access-group control, Claude Code request validation, and optional monitoring for a Docker Swarm deployment managed through Portainer.

## Architecture

This repository is deployed as multiple Docker Swarm stacks:

### Application data stack (`llmproxy-data.yaml`)
- `db`: PostgreSQL database for LiteLLM state, usage logs, and model configuration

### Application stack (`llmproxy.yaml`)
- `cli-proxy-api`: Anthropic-compatible proxy and auth service
- `litellm`: Core routing layer and LiteLLM admin UI

### Monitoring stack (`monitoring/netdata.yaml`)
- `netdata`: Host and container monitoring dashboard
- `config-generator`: Sidecar that watches Docker labels and generates Netdata collector configs

### Networks
- `internal`: private overlay network between application services and PostgreSQL
- `public`: external Traefik network for HTTPS routing
- `monitoring`: shared overlay network used by Netdata auto-discovery

## Prerequisites

```sh
# Install ptctools
uv tool install ptctools --from git+https://github.com/tamntlib/ptctools.git
```

## Installation

### 1. Install Portainer CE with Docker Swarm

#### Set DNS records for Portainer

Add the following record to your DNS:

- `portainer.example.com`

#### Copy file `portainer.yaml` to server

```sh
scp portainer/portainer.yaml root@<ip>:/root/portainer.yaml
```

#### SSH to server

##### Install Docker

<https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository>

##### Install Portainer CE with Docker Swarm

```sh
docker swarm init
LETSENCRYPT_EMAIL=<email> PORTAINER_HOST=<host> docker stack deploy -c /root/portainer.yaml portainer
```

### 2. Deploy the monitoring stack

Deploy this first so the shared `monitoring` overlay network exists before the application stacks join it.

#### Set DNS records

Add the following record to your DNS:

- `netdata.example.com`

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
ptctools docker config set -n monitoring_config-generator-script -f 'monitoring/scripts/netdata-config-generator.sh'
ptctools docker stack deploy -n monitoring -f 'monitoring/netdata.yaml' --ownership team
```

### 3. Deploy the application stacks from your local machine

#### Set DNS records

Add the following records to your DNS:

- `llm.example.com` (LiteLLM)
- `cli-proxy-api.llm.example.com` (CLIProxyAPI)

#### Set environment variables

Copy `.env.example` to `.env` and fill in the values:

```sh
cp .env.example .env
```

Required environment variables:
- `DB_USER`, `DB_PASSWORD`, `DB_NAME`: PostgreSQL credentials
- `LITELLM_HOST`, `LITELLM_MASTER_KEY`, `LITELLM_SALT_KEY`: LiteLLM configuration
- `CLI_PROXY_API_HOST`: CLIProxyAPI hostname

Optional environment variables used by the stack:
- `CLAUDE_CODE_MODELS`: Comma-separated model names that should enforce Claude Code checks
- `CLAUDE_CODE_MIN_VERSION`: Minimum allowed Claude Code version for those models
- `SLACK_WEBHOOK_URL`: LiteLLM Slack webhook

#### Upload configs and deploy

```sh
export PORTAINER_URL=https://portainer.example.com
export PORTAINER_ACCESS_TOKEN=<token>

ptctools docker config set -n llmproxy_litellm-config-yaml -f 'configs/litellm.yaml' --ownership team
ptctools docker config set -n llmproxy_litellm-claude-code-hook-py -f 'configs/claude_code_hook.py' --ownership team
ptctools docker config set -n llmproxy_cli-proxy-api-config-yaml -f 'configs/cli-proxy-api.yaml' --ownership team

ptctools docker stack deploy -n llmproxy-data -f 'llmproxy-data.yaml' --ownership team
ptctools docker stack deploy -n llmproxy -f 'llmproxy.yaml' --ownership team
```

## LiteLLM management

```sh
cd litellm_scripts

# Generate a resolved config from config.json + config.local.json
python3 gen_config.py

# Full sync of credentials, models, aliases, fallbacks, and public model hub
python3 config.py --only credentials,models,aliases,fallbacks,public_model_hub --force --prune

# Sync specific components
python3 config.py --only models --force
python3 config.py --only aliases,fallbacks,public_model_hub
python3 config.py --only public_model_hub

# Create a LiteLLM user and API key
python3 create_api_key.py user@example.com
python3 create_api_key.py user@example.com --alias my-key
```

Required environment variables in `litellm_scripts/.env`:
- `LITELLM_API_KEY`
- `LITELLM_BASE_URL`

## Configuration files

| File | Description |
|------|-------------|
| `llmproxy-data.yaml` | PostgreSQL Docker Swarm stack |
| `llmproxy.yaml` | Application Docker Swarm stack for LiteLLM and CLIProxyAPI |
| `monitoring/netdata.yaml` | Monitoring stack with Netdata and the label-watching config generator |
| `configs/litellm.yaml` | LiteLLM runtime config (callbacks, DB batching, connection pool settings) |
| `configs/cli-proxy-api.yaml` | CLIProxyAPI runtime config |
| `configs/claude_code_hook.py` | LiteLLM callback that enforces Claude Code User-Agent and minimum version rules |
| `litellm_scripts/config.json` | Base provider/model/alias/fallback/public-model-hub config |
| `litellm_scripts/config.local.json` | Local overrides including API keys (gitignored, deep-merged with `config.json`) |
| `litellm_scripts/config.gen.json` | Generated resolved config output from `gen_config.py` |
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

Each interface may define `model_name_prefix` to control derived model group names. When omitted, it defaults to the interface name.

```json
{
  "providers": {
    "my-provider": {
      "interfaces": {
        "anthropic": {
          "model_name_prefix": "anthropic",
          "models": {
            "claude-sonnet-4-6": null
          }
        }
      }
    }
  }
}
```

With no explicit `model_name`, the generated model group name becomes `<model_name_prefix>/<model-id>`. If `model_name` is set on a model, it still wins.

### Model-level `access_groups`

Individual models can override the provider-level `access_groups` by specifying `access_groups` in their model config:

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

### Auto-discovery

Services can self-register for PostgreSQL monitoring by adding Docker labels:

```yaml
deploy:
  labels:
    - netdata.postgres.name=my_database
    - netdata.postgres.dsn=postgresql://user:pass@host:5432/dbname

networks:
  - monitoring
```

The service must also join the shared `monitoring` network so the Netdata stack can reach it.
