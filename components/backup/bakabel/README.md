# Bakabel: optional automated backups

The independent `backup` preset selects only `backup/bakabel`; it never deploys
application services or joins `public`. It is not inherited by `all` or `default`.

Bakabel v0.3.1 is configured **100% through environment variables** (no YAML file,
no manual `restic init`, and no manual host bind directories required):

- **Pinned image (`linux/amd64`):**
  `ghcr.io/tamntlib/bakabel@sha256:c80fe490b5ad3f0b857ce442d92d41589e172258f1696a90d97c98095da07323`
  (built from `tamntlib/bakabel` commit `f4158a3327d07909bb5bbe3d4265530bd00d6315`, tag `v0.3.1`).
- **Safe auto-init:** on the first run, if the configured repository does not exist
  and is empty, Bakabel runs `restic init` automatically and pins `repository_id`
  in `bakabel-state`. If storage later disappears or changes, Bakabel refuses to
  re-initialize and fails closed.
- **Schedule:** daily at `02:00 Asia/Ho_Chi_Minh` (`BAKABEL_SCHEDULE` / `BAKABEL_TZ`).
- **Retention:** disabled by default (`BAKABEL_RETENTION=` empty). Enable only
  after verifying a restore (for example `BAKABEL_RETENTION=7d,4w,6m`).

## 1. Configure `.env`

Set the required encryption passphrase in root `.env`:

```ini
BAKABEL_RESTIC_PASSWORD=<strong-passphrase-stored-off-host>
```

> On a single-node Swarm, `node.role == manager` is sufficient. If you run a
> multi-manager Swarm, pin Bakabel to the data manager node in
> `components/backup/bakabel/compose.local.yaml` (`node.id == <manager-node-id>`).

Optional overrides in `.env`:

- `BAKABEL_REPOSITORY` (default `/repository` on named volume `bakabel-repo`; set
  `s3:https://...` + `BAKABEL_AWS_ACCESS_KEY_ID` / `BAKABEL_AWS_SECRET_ACCESS_KEY` /
  `BAKABEL_AWS_DEFAULT_REGION` for off-host backups)
- `BAKABEL_SCOPE` (default empty = all labeled services on that Swarm manager; e.g. `llmproxy-data`)
- `BAKABEL_SCHEDULE` (default `0 2 * * *`), `BAKABEL_TZ` (default `Asia/Ho_Chi_Minh`)
- `BAKABEL_RETENTION` (default empty / off, e.g. `7d,4w,6m`)
- `BAKABEL_WEBHOOK_URL` (optional failure alert webhook)

> Always store an independent copy of `BAKABEL_RESTIC_PASSWORD` outside the server.
> A local repository volume shares the host's failure boundary; use S3-compatible
> storage for off-host disaster recovery.

## 2. Label services to back up

Bakabel discovers opted-in services via Swarm `deploy.labels` on the same manager
node (`replicas: 1`).

**PostgreSQL** (`components/llmproxy-data/postgres/compose.yaml` already includes this label):

```yaml
services:
  db:
    deploy:
      labels:
        bakabel.type: postgres
```

**SQLite** (`components/llmproxy/cli-proxy-api-usage/compose.yaml` already includes these labels — uses WAL-safe online `sqlite3 .backup` + `PRAGMA integrity_check`):

```yaml
services:
  cli-proxy-api-usage:
    deploy:
      labels:
        bakabel.type: sqlite
        bakabel.paths: /data/app.db
```

**Named Docker volume** (non-database files only; PGDATA is refused):

```yaml
services:
  files:
    deploy:
      labels:
        bakabel.type: volume
        bakabel.paths: /data
```

## 3. Validate, deploy, and verify

```bash
uv run llmproxy inputs --preset backup --stack backup --no-local-overrides
uv run llmproxy validate --preset backup --stack backup --no-local-overrides
uv run llmproxy deploy --preset backup --stack backup --driver docker --no-local-overrides --dry-run
uv run llmproxy deploy --preset backup --stack backup --driver docker --no-local-overrides
```

Common operations inside the running `backup_bakabel` container:

```bash
# Preview discovered targets
docker exec <container> bakabel plan

# Trigger an immediate backup (auto-initializes empty repo on first run)
docker exec <container> bakabel run

# Check health & list snapshots
docker exec <container> bakabel health
docker exec <container> bakabel snapshots

# Restore into a separate empty PostgreSQL container or empty volume
docker exec <container> bakabel restore <snapshot-id> --container <empty-pg-container>
docker exec <container> bakabel restore <snapshot-id> --volume <empty-volume>
```

> Mounting `/var/run/docker.sock` grants root-equivalent Docker Engine authority
> even when mounted `:ro`. Protect `.env`, `bakabel-state`, and the repository.
