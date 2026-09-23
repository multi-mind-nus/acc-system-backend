# acc-system-backend

FastAPI backend for the accounting document collection system.

## Local checks

```bash
uv sync --frozen
uv run pytest tests/test_health.py
```

Account tests erase their PostgreSQL tables and Redis database. Use disposable services, never `deploy/.env.prod` or the running application's database. The test guard requires `ENVIRONMENT=test`, PostgreSQL database `acc_test`, and Redis DB `15`.

```bash
docker run --rm -d --name acc-test-postgres -p 127.0.0.1:15432:5432 \
  -e POSTGRES_DB=acc_test -e POSTGRES_USER=acc -e POSTGRES_PASSWORD=acc_test postgres:17.6-alpine
docker run --rm -d --name acc-test-redis -p 127.0.0.1:16379:6379 redis:8.2.1-alpine
docker exec acc-test-postgres pg_isready -U acc -d acc_test
```

Wait until PostgreSQL reports `accepting connections`, then run in a subshell so test settings do not remain in your terminal:

```bash
(
  export ENVIRONMENT=test
  export DATABASE_URL=postgresql+psycopg://acc:acc_test@127.0.0.1:15432/acc_test
  export REDIS_URL=redis://127.0.0.1:16379/15
  export JWT_SECRET=test-only-secret-with-at-least-32-characters
  uv run alembic upgrade head
  uv run pytest
)
docker stop acc-test-postgres acc-test-redis
```

The final command removes only those disposable containers and their test data. Invitation tokens are returned to authorized callers; password-reset tokens are returned only outside production. Email delivery is a later phase.

## Local deployment

Run the full stack after copying the deployment environment file:

```bash
cp deploy/.env.example deploy/.env.prod
docker build -t acc-system-backend:local .
docker build -t acc-system-nginx:local deploy/nginx
docker build -t acc-system-agent:local ../acc-system-agent
docker compose --env-file deploy/.env.prod -f deploy/compose.yml up -d
```

Then open `http://localhost` and check:

```bash
curl http://localhost/api/v1/health/live
curl http://localhost/api/v1/health/ready
```

See [deploy/README.md](deploy/README.md) for ECR and optional TLS deployment.
