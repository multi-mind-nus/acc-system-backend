# acc-system-backend

FastAPI deployment baseline for the accounting document collection system.

## Local checks

```bash
uv sync --frozen
uv run pytest
```

Run the full stack after copying the deployment environment file:

```bash
cp deploy/.env.example deploy/.env.prod
docker build -t acc-system-backend:local .
docker build -t acc-system-nginx:local deploy/nginx
docker compose --env-file deploy/.env.prod -f deploy/compose.yml up -d
```

Then open `http://localhost` and check:

```bash
curl http://localhost/api/v1/health/live
curl http://localhost/api/v1/health/ready
```

See [deploy/README.md](deploy/README.md) for ECR and optional TLS deployment.
