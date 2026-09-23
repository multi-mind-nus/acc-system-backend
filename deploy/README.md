# Deployment

## One-time AWS and GitHub setup

No AWS access key is stored in GitHub. Create these ECR repositories in `ap-southeast-1`:

- `acc-system-backend`
- `acc-system-frontend`
- `acc-system-nginx`
- `acc-system-agent`

Create the GitHub Actions OIDC provider and one IAM role using the templates in `deploy/aws/`. Replace `<AWS_ACCOUNT_ID>` before applying them. The trust-policy template includes the immutable GitHub owner and repository IDs for the frontend, backend and Agent repositories.

Configure all three GitHub repositories with the secret `AWS_ROLE_ARN` and variable `AWS_REGION=ap-southeast-1`. Configure repository-name variables as follows:

| GitHub repository | Variable | Value |
| --- | --- | --- |
| backend | `ECR_BACKEND_REPOSITORY` | `acc-system-backend` |
| backend | `ECR_NGINX_REPOSITORY` | `acc-system-nginx` |
| frontend | `ECR_FRONTEND_REPOSITORY` | `acc-system-frontend` |
| agent | `ECR_AGENT_REPOSITORY` | `acc-system-agent` |

For the current Lightsail server, send a short-lived ECR login token from an SSO-authenticated workstation before pulling images:

```bash
aws ecr get-login-password --profile acc-system --region ap-southeast-1 \
  | ssh ubuntu@13.213.135.223 \
      'docker login --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.ap-southeast-1.amazonaws.com'
```

The ECR token is temporary. Do not install permanent AWS access keys or put them in `.env.prod`.

## Local or staging HTTP

Build the four application images first. The frontend command runs in its own repository.

```bash
docker build --build-arg APP_VERSION=local -t acc-system-backend:local ..
docker build -t acc-system-nginx:local nginx
docker build --build-arg APP_VERSION=local -t acc-system-frontend:local ../../acc-system-frontend
docker build --build-arg APP_VERSION=local -t acc-system-agent:local ../../acc-system-agent
cp .env.example .env.prod
docker compose --env-file .env.prod -f compose.yml up -d
docker compose --env-file .env.prod -f compose.yml --profile tools run --rm bootstrap
```

Run commands from the `deploy/` directory. Set `JWT_SECRET` to a random value (for example, `openssl rand -hex 32`) and set the bootstrap administrator fields before the first bootstrap run. For production, replace the four local image values with immutable ECR SHA tags and replace all example passwords.

## Agent baseline (B6.1 + A1)

For **B6.3/A3 post-submission analysis** also set `AGENT_REVIEW_PROVIDER=MOCK` locally. Build backend/Agent with version `b6.3-a3-local` and restart backend, worker and agent. Submitted requests create REVIEW runs; OFF creates none. This release exposes analysis suggestions only and never applies AI decisions. Real model integration requires REVIEW_PROVIDER=REMOTE plus the Agent README contract. Never enable MOCK in production. No new database revision is required after `0010_classification_confirmation`.

For the current **B6.2/A2 UI acceptance** on a local development stack, additionally set `AGENT_CLASSIFICATION_PROVIDER=MOCK` and keep `ENVIRONMENT=development`. Rebuild backend and Agent, run `docker compose --env-file deploy/.env.prod -f deploy/compose.yml up -d backend worker agent`, then reload Nginx if its upstream container IP changed. Agent MOCK readiness is healthy without credentials; the UI explicitly labels simulated results. Production defaults to DISABLED and rejects MOCK. No trained-model accuracy is claimed. `REMOTE` requires the provisional contract in the Agent README to be adapted to the actual provider.

`AGENT_IMAGE` defaults to `acc-system-agent:local`; `AGENT_URL` defaults to `http://agent:8000`.
Only Agent receives `MODEL_API_URL`, `MODEL_HEALTH_URL` and `MODEL_API_KEY`. No model key is needed for local baseline acceptance. Its `/health/live` returns 200 while `/health/ready` returns `503 MODEL_NOT_CONFIGURED` until a real provider is configured. Backend readiness does not depend on Agent or the model.

Agent uses UID 10001, a read-only root filesystem and read-only documents volume, with no host port or database/Redis credentials. The Compose `internal` network is a private bridge with outbound connectivity, allowing the future remote model connection.

From the backend repository, inspect the running baseline:

```bash
docker compose --env-file deploy/.env.prod -f deploy/compose.yml ps
docker compose --env-file deploy/.env.prod -f deploy/compose.yml exec -T backend alembic current
docker compose --env-file deploy/.env.prod -f deploy/compose.yml exec -T backend python -c 'import os, urllib.request; print(urllib.request.urlopen(os.environ["AGENT_URL"] + "/health/live").read().decode())'
docker compose --env-file deploy/.env.prod -f deploy/compose.yml exec -T agent python -c 'import httpx; r = httpx.get("http://127.0.0.1:8000/health/ready"); print(r.status_code, r.json())'
docker inspect acc-system-agent-1 --format '{{json .HostConfig.PortBindings}} {{range .Mounts}}{{.Destination}} writable={{.RW}} {{end}}'
```

Expected migration: `0009_b6_ai_baseline`. Existing requests migrate to `SUGGEST`; new requests default to `AUTO_REVIEW` and both thresholds `0.980`. These are stored policies; B6.1 does not yet run classification or automatic reviews. No notifications are sent.

Before production rollout, create the Agent ECR repository and update the actual IAM policies/repository variables using the templates above. A1 adds the workflow but local acceptance does not publish images or modify AWS IAM.

Set `FRONTEND_ORIGIN` to the exact browser origin, including the scheme and any non-default port. `http://localhost` is for local acceptance only; visiting through a different host without changing this value prevents refresh and logout.

For production document uploads, set `CLAMAV_HOST=clamav` and start Compose with `--profile malware-scan`. The Worker deliberately refuses to release files without ClamAV when `ENVIRONMENT=production`; development uses the built-in EICAR check for local acceptance only.

The backend is reachable only on the Compose network and trusts the forwarding headers that Nginx overwrites with the actual connection address. Do not publish the backend port or attach untrusted containers to this network. If another proxy is added in front of Nginx, configure its trusted addresses explicitly before relying on client-IP rate limits.

## TLS (required for public production)

1. Put `fullchain.pem` and `privkey.pem` in `deploy/certs/`.
2. Copy `nginx/tls/https.conf.example` to `nginx/tls/runtime/https.conf` and set the domain.
3. Set `FRONTEND_ORIGIN=https://your-domain` and `COOKIE_SECURE=true` in `.env.prod`. Use `COOKIE_SECURE=false` only for local or restricted HTTP acceptance.
4. Block public HTTP access or redirect it to HTTPS at the entry point; the base HTTP listener is retained for local acceptance and health checks.
5. Start with both Compose files:

```bash
docker compose --env-file .env.prod -f compose.yml -f compose.tls.yml up -d
```

Certificate renewal is owned by the server or an external load balancer; private keys are never built into the Nginx image.

## Release

Run these commands on the server from `/opt/acc-system` after refreshing its ECR login:

For a TLS deployment, include `-f compose.tls.yml` after `-f compose.yml` in every command below; omitting it from `up` would remove the HTTPS port and certificate mounts.

```bash
docker compose --env-file .env.prod -f compose.yml pull
docker compose --env-file .env.prod -f compose.yml run --rm migrate
docker compose --env-file .env.prod -f compose.yml up -d --remove-orphans
```
