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

For **B6.4/F7.3 post-submission review** also set `AGENT_REVIEW_PROVIDER=MOCK` locally. Build Backend with version `b6.4-f7.3-local` and restart backend and worker; rebuild Agent only when its code changes. Submitted requests create REVIEW runs; `AUTO_REVIEW` automatically returns high-confidence issues and marks high-confidence passes satisfied, but the whole request still requires accountant confirmation; `SUGGEST` only records suggestions, and `OFF` creates none. Waiver and final approval remain manual. For Novita inference use `AGENT_REVIEW_PROVIDER=DEEPSEEK`; `REMOTE` remains the separate provisional custom-model contract. Never enable MOCK in production. No new database revision is required after `0010_classification_confirmation`.

For the current **B6.2/A2 UI acceptance** on a local development stack, additionally set `AGENT_CLASSIFICATION_PROVIDER=MOCK` and keep `ENVIRONMENT=development`. Rebuild backend and Agent, run `docker compose --env-file deploy/.env.prod -f deploy/compose.yml up -d backend worker agent`, then reload Nginx if its upstream container IP changed. Agent MOCK readiness is healthy without credentials; the UI explicitly labels simulated results. Production defaults to DISABLED and rejects MOCK. No trained-model accuracy is claimed. `REMOTE` requires the provisional contract in the Agent README to be adapted to the actual provider.

`AGENT_IMAGE` defaults to `acc-system-agent:local`; `AGENT_URL` defaults to `http://agent:8000`.

For Novita OCR→Flash, use the updated Agent image and set `NOVITA_API_KEY` in the ignored `deploy/.env.prod`; keep `MODEL_API_KEY`/`OCR_API_KEY` empty unless intentionally overriding the shared credential. Set `AGENT_CLASSIFICATION_PROVIDER=DEEPSEEK` and `AGENT_REVIEW_PROVIDER=DEEPSEEK` only when ready to process real jobs. Defaults remain disabled. The Agent uses Novita's `deepseek/deepseek-ocr-2` for every image/PDF page and `deepseek/deepseek-v4.1-flash` for structured classification/review. Do not place provider keys in Compose YAML or GitHub variables visible to untrusted workflows.
Only Agent receives `NOVITA_API_KEY`, optional `OCR_*` and `MODEL_*` provider settings. No model key is needed for local MOCK acceptance. Its `/health/live` returns 200 while `/health/ready` returns 503 if a selected real provider is unconfigured or unavailable. Backend readiness does not depend on Agent or the model.

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

1. Obtain the first certificate with Certbot's standalone HTTP challenge, mounting `deploy/certs/` as `/etc/letsencrypt`. Stop Nginx briefly while Certbot binds port 80, and start it again even if issuance fails.
2. Copy `nginx/tls/https.conf.example` to `nginx/tls/runtime/https.conf` and replace `example.com` with the domain. Mount `deploy/acme/` for subsequent webroot renewals. The certificate and private key stay outside the image.
3. Set `FRONTEND_ORIGIN=https://your-domain` and `COOKIE_SECURE=true` in `.env.prod`. Use `COOKIE_SECURE=false` only for local or restricted HTTP acceptance.
4. The domain-specific Nginx server redirects HTTP to HTTPS. The default HTTP listener remains for local health checks. For a Cloudflare-proxied domain, select **Full (strict)** in Cloudflare SSL/TLS settings so it validates the origin certificate.
5. Start with both Compose files:

```bash
docker compose --env-file .env.prod -f compose.yml -f compose.tls.yml up -d
```

Certificate renewal is owned by the server or an external load balancer; private keys are never built into the Nginx image.
For the Folio Lightsail server, configure the certificate's renewal method as webroot, then run `renew-tls.sh` daily from root's crontab. It renews only when fewer than 30 days remain and reloads Nginx without stopping the website. The deployed root crontab runs it at `18:17 UTC` and records output in `/var/log/folio-tls-renew.log`.

## Release

Run these commands on the server from `/opt/acc-system` after refreshing its ECR login:

For a TLS deployment, include `-f compose.tls.yml` after `-f compose.yml` in every command below; omitting it from `up` would remove the HTTPS port and certificate mounts.

```bash
docker compose --env-file .env.prod -f compose.yml pull
docker compose --env-file .env.prod -f compose.yml run --rm migrate
docker compose --env-file .env.prod -f compose.yml up -d --remove-orphans
```
