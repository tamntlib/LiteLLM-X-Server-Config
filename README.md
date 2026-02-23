# LLM Proxy

A self-hosted LLM proxy service combining [LiteLLM](https://github.com/BerriAI/litellm) and [Antigravity Manager](https://github.com/lbjlaq/antigravity-manager) to enable integration with a wider variety of LLM providers. Designed for enterprises that need centralized control over accounts and API keys. Includes monitoring capabilities and is deployed on Docker Swarm, managed via Portainer.

## Architecture

**Docker Swarm Stack** (`llmproxy.yaml`) deployed via Portainer:
- `litellm`: Core proxy server handling API requests
- `antigravity-manager`: Proxy for Anthropic Claude models
- `db`: PostgreSQL database for LiteLLM usage logs and model configurations
- `db-cleanup`: Scheduled job to prune old spend logs (prevents disk exhaustion)

**Networks**: Internal overlay network for inter-service communication, external public network with Traefik for HTTPS routing.

## Disk Management

The stack includes automatic cleanup mechanisms to prevent disk exhaustion:

### Database Cleanup

The `db-cleanup` service runs weekly (Sunday 3:00 AM) to prune old spend logs:
- Deletes logs older than 90 days (configurable via `DB_CLEANUP_RETENTION_DAYS`)
- Runs `VACUUM ANALYZE` to reclaim disk space
- Uses [swarm-cronjob](https://github.com/crazy-max/swarm-cronjob) for scheduling

To run cleanup manually:
```sh
docker service scale llmproxy_db-cleanup=1
```

### Netdata Metrics Storage

Netdata is configured to limit disk usage to 10GB (`monitoring/configs/netdata.conf`), providing approximately 2-4 weeks of metrics retention.

## Prerequisites

```sh
# Install ptctools
uv tool install ptctools --from git+https://github.com/tamntlib/ptctools.git
```

## Installation

### 1. Install Portainer CE with Docker Swarm

#### Set DNS records for Portainer

Add the following records to your DNS:

- portainer.example.com

#### Copy file portainer.yaml to server

```sh
scp portainer/portainer.yaml root@<ip>:/root/portainer.yaml
```

#### SSH to server

##### Install docker

<https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository>

##### Install Portainer CE with Docker Swarm

```sh
docker swarm init
LETSENCRYPT_EMAIL=<email> PORTAINER_HOST=<host> docker stack deploy -c /root/portainer.yaml portainer
```

### 2. Deploy LLM Proxy from local machine

#### Set DNS records

Add the following records to your DNS:

- llm.example.com (for LiteLLM)
- cli-proxy-api.llm.example.com (for CLIProxyAPI)
- antigravity-manager.llm.example.com (for Antigravity Manager)

#### Set environment variables

Copy `.env.example` to `.env` and fill in the values:

```sh
cp .env.example .env
```

Required environment variables:
- `DB_USER`, `DB_PASSWORD`, `DB_NAME`: PostgreSQL credentials
- `LITELLM_MASTER_KEY`, `LITELLM_HOST`: LiteLLM configuration
- `ANTIGRAVITY_MANAGER_HOST`, `ANTIGRAVITY_MANAGER_API_KEY`, `ANTIGRAVITY_MANAGER_WEB_PASSWORD`: Antigravity Manager configuration

#### Set config and deploy stack

```sh
export PORTAINER_URL=https://portainer.example.com
export PORTAINER_ACCESS_TOKEN=<token>

# Set config
ptctools docker config set -n llmproxy_litellm-config-yaml -f 'configs/litellm.yaml'
ptctools docker config set -n llmproxy_cli-proxy-api-config-yaml -f 'configs/cli-proxy-api.yaml'

# Deploy stacks
ptctools docker stack deploy -n llmproxy-data -f 'llmproxy-data.yaml' --ownership team
ptctools docker stack deploy -n llmproxy -f 'llmproxy.yaml' --ownership team
```

## LiteLLM Management

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

Requires environment variables in `litellm_scripts/.env`: `LITELLM_API_KEY`, `LITELLM_BASE_URL`

## Configuration Files

| File | Description |
|------|-------------|
| `llmproxy.yaml` | Docker Stack definition with all services and Traefik labels |
| `configs/litellm.yaml` | LiteLLM internal config (batch writes, connection pools, logging) |
| `litellm_scripts/config.json` | Base config defining providers, model groups, aliases, and fallbacks |
| `litellm_scripts/config.local.json` | Local overrides including API keys (gitignored, deep-merged with config.json) |
| `.env` | Environment variables (DB credentials, hostnames, API keys) |

### Local Configuration (config.local.json)

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

This file is deep-merged with `config.json`, so you only need to specify overrides (like API keys).

### Model-level access_groups

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

## Backup and Restore

```sh
# Volume backup/restore (uses Duplicati)
ptctools docker volume backup -v vol1,vol2 -o s3://mybucket
ptctools docker volume restore -i s3://mybucket/vol1  # volume name derived from URI path
ptctools docker volume restore -v vol1 -i s3://mybucket/vol1  # explicit volume name

# Database backup/restore (uses minio/mc for S3) or can backup/restore db volume like above
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

Netdata monitoring stack with auto-discovery for system, container, and database metrics.

### Deploy Monitoring Stack

#### Set DNS records

Add the following record to your DNS:

- netdata.example.com

#### Set environment variables

Copy `monitoring/.env.example` to `monitoring/.env` and fill in the values:

```sh
cp monitoring/.env.example monitoring/.env
```

Required environment variables:
- `NETDATA_HOST`: Hostname for Netdata dashboard
- `NETDATA_BASIC_AUTH`: Basic auth credentials (generate with `htpasswd -nb admin yourpassword | sed -e s/\\$/\\$\\$/g`)

#### Create secrets and deploy

```sh
cd monitoring

# Upload configs
ptctools docker config set -n monitoring_netdata-conf -f 'configs/netdata.conf'
ptctools docker config set -n monitoring_config-generator-script -f 'scripts/netdata-config-generator.sh'

# Deploy monitoring stack
ptctools docker stack deploy -n monitoring -f 'netdata.yaml' --ownership team
```

### Auto-Discovery

Services can self-register for PostgreSQL monitoring by adding Docker labels:

```yaml
deploy:
  labels:
    - netdata.postgres.name=my_database
    - netdata.postgres.dsn=postgresql://user:pass@host:5432/dbname

networks:
  - monitoring
```

The service must also join the `monitoring` network. See `CLAUDE.md` for full details.
