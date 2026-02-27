# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

LiteLLM-CLIProxyAPI is a self-hosted LLM proxy service combining [LiteLLM](https://github.com/BerriAI/litellm) and [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI). It provides centralized control over API keys, usage monitoring, and model fallback strategies for multiple LLM providers (OpenAI, Gemini, Anthropic).

## Architecture

**Docker Swarm Stack** (`llmproxy.yaml`) deployed via Portainer:
- `litellm`: Core proxy server handling API requests (LiteLLM v1.81.0)
- `antigravity-manager`: Proxy for Anthropic Claude models using [Antigravity Manager](https://github.com/lbjlaq/antigravity-manager)
- `db`: PostgreSQL database for LiteLLM usage logs and model configurations

**Networks**: Internal overlay network for inter-service communication, external public network with Traefik for HTTPS routing.

## Common Commands

### Prerequisites
```sh
uv tool install ptctools --from git+https://github.com/tamntlib/ptctools.git

# Set environment variables (ptctools reads these automatically)
export PORTAINER_URL=https://portainer.example.com
export PORTAINER_ACCESS_TOKEN=<token>
```

### Deploy Stack
```sh
# Set configs
ptctools docker config set -n llmproxy_cli_proxy_api_config_yaml -f 'configs/cli_proxy_api.yaml'
ptctools docker config set -n llmproxy_litellm_config_yaml -f 'configs/litellm.yaml'

# Deploy
ptctools docker stack deploy -n llmproxy -f 'llmproxy.yaml' --ownership team
```

### LiteLLM Management (litellm_scripts/)
```sh
cd litellm_scripts

# Full sync of credentials, models, aliases, and fallbacks
python3 config.py --only credentials,models,aliases,fallbacks --force --prune

# Sync specific components
python3 config.py --only models --force
python3 config.py --only aliases,fallbacks

# Create API key
python3 create_api_key.py
```

Requires environment variables: `LITELLM_API_KEY`, `LITELLM_BASE_URL` (from `litellm_scripts/.env`)

### Backup/Restore
```sh
# Volume backup
ptctools docker volume backup -v vol1,vol2 -o s3://mybucket

# Database backup
ptctools docker db backup -c container_id -v db_data \
  --db-user postgres --db-name mydb -o s3://mybucket/backups/db.sql.gz
```

## Key Configuration Files

- `llmproxy.yaml`: Docker Stack definition with all services and Traefik labels
- `configs/litellm.yaml`: LiteLLM internal config (batch writes, connection pools, logging)
- `litellm_scripts/config.json`: Base config defining providers, model groups, aliases, and fallbacks
- `litellm_scripts/config.local.json`: Local overrides including API keys (gitignored)
- `.env`: Environment variables (DB credentials, hostnames, API keys)

## LiteLLM config.json Structure

The `litellm_scripts/config.json` defines:
- **providers**: Service definitions with `api_base`, `interfaces` (openai/gemini/anthropic), `access_groups`, and `models`
- **aliases**: Model name mappings (e.g., `claude-opus-4-5` -> `anthropic/claude-opus-4-5-20251101`)
- **fallbacks**: Automatic routing to alternative models on failure

Provider configs support `$extend` directive to inherit from another provider with overrides.

Individual models can override the provider-level `access_groups` by specifying `access_groups` in their model config. If not set, the provider-level value is used.

### Local Configuration (config.local.json)

Create `config.local.json` to add API keys and local overrides (deep-merged with `config.json`):
```json
{
  "providers": {
    "my-provider": {
      "api_key": "sk-your-api-key-here"
    }
  }
}
```

## Monitoring (monitoring/)

Netdata monitoring stack for system, container, and database metrics with auto-discovery.

### Deploy Monitoring Stack
```sh
# Upload config generator script
ptctools docker config set -n monitoring_config-generator-script -f 'monitoring/scripts/netdata-config-generator.sh'

# Deploy stack
ptctools docker stack deploy -n monitoring -f 'monitoring/netdata.yaml' --ownership team
```

### Monitoring Coverage
- **Host system**: CPU, RAM, disk, network via `/proc` and `/sys` mounts
- **Docker containers**: Auto-discovered via Docker socket
- **PostgreSQL**: Auto-discovered via Docker labels (see below)
- **Application logs**: JSON logs from `/host/var/log`

### Auto-Discovery with Docker Labels

Services can self-register for monitoring by adding Docker labels. The config-generator sidecar watches for these labels and automatically generates Netdata collector configs.

**PostgreSQL monitoring:**
```yaml
deploy:
  labels:
    - netdata.postgres.name=my_database
    - netdata.postgres.dsn=postgresql://user:pass@host:5432/dbname
```

The service must also join the `monitoring` network:
```yaml
networks:
  - monitoring

networks:
  monitoring:
    name: ${MONITORING_NETWORK:-monitoring}
    external: true
```

### Configuration Files
- `monitoring/netdata.yaml`: Docker Swarm stack definition
- `monitoring/scripts/netdata-config-generator.sh`: Auto-discovery script
- `monitoring/.env.example`: Environment variables template
