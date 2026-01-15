# LiteLLM-CLIProxyAPI

A self-hosted LLM proxy service combining [LiteLLM](https://github.com/BerriAI/litellm) and [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) to enable integration with a wider variety of LLM providers. Designed for enterprises that need centralized control over accounts and API keys. Includes monitoring capabilities and is deployed on Docker Swarm, managed via Portainer.

## Prerequisites

```sh
# Install ptctools
uv tool install ptctools --from git+https://github.com/tamntlib/ptctools.git
```

## Install LiteLLM-CLIProxyAPI

### 1. Install Portainer CE with Docker Swarm

#### Set DNS records for Portainer

Add the following records to your DNS:

- portainer.example.com

#### Copy file portainer.yml to server

```sh
scp portainer/portainer.yml root@<ip>:/root/portainer.yml
```

#### SSH to server

##### Install docker

<https://docs.docker.com/engine/install/ubuntu/#install-using-the-repository>

##### Install Portainer CE with Docker Swarm

```sh
docker swarm init
LETSENCRYPT_EMAIL=<email> PORTAINER_HOST=<host> docker stack deploy -c /root/portainer.yml portainer
```

### 2. Deploy LiteLLM-CLIProxyAPI from local machine

#### Set DNS records for LiteLLM-CLIProxyAPI

Add the following records to your DNS:

- llm.example.com
- cli-proxy-api.llm.example.com
- cli-proxy-api-2.llm.example.com (optional)

#### Set environment variables

Copy .env.example to .env and fill in the values
Copy cli_proxy_api.example.yaml to cli_proxy_api.yaml, cli_proxy_api_2.yaml and fill in the values (secret-key, ...)

> **Note:** If you don't need `cli-proxy-api-2`, you can skip creating `cli_proxy_api_2.yaml` and remove the related DNS record, config set command, and service from `llmproxy.yaml`.

#### Set config and deploy stack

```sh
export PORTAINER_URL=https://portainer.example.com
export PORTAINER_ACCESS_TOKEN=<token>

# Set config
ptctools config set -u $PORTAINER_URL -n cli_proxy_api_config_yaml -f 'cli_proxy_api.yaml'
ptctools config set -u $PORTAINER_URL -n cli_proxy_api_2_config_yaml -f 'cli_proxy_api_2.yaml'
ptctools config set -u $PORTAINER_URL -n prometheus_yml -f 'prometheus.yml'

# Deploy stack
ptctools stack deploy -u $PORTAINER_URL -n llmproxy -f 'llmproxy.yaml' --ownership team
```

## Backup and restore

```sh
# Volume backup/restore (uses Duplicati)
ptctools volume backup -u $PORTAINER_URL -v vol1,vol2 -o s3://mybucket
ptctools volume restore -u $PORTAINER_URL -i s3://mybucket/vol1  # volume name derived from URI path
ptctools volume restore -u $PORTAINER_URL -v vol1 -i s3://mybucket/vol1  # explicit volume name

# Database backup/restore (uses minio/mc for S3) or can backup/restore db volume like above
ptctools db backup -u $PORTAINER_URL -c container_id -v db_data \
  --db-user postgres --db-name mydb -o backup.sql.gz
ptctools db backup -u $PORTAINER_URL -c container_id -v db_data \
  --db-user postgres --db-name mydb -o s3://mybucket/backups/db.sql.gz

ptctools db restore -u $PORTAINER_URL -c container_id -v db_data \
  --db-user postgres --db-name mydb -i backup.sql.gz
ptctools db restore -u $PORTAINER_URL -c container_id -v db_data \
  --db-user postgres --db-name mydb -i s3://mybucket/backups/db.sql.gz
```
