# Deployment

## One-time AWS and GitHub setup

No AWS access key is stored in GitHub. Create these ECR repositories in `ap-southeast-1`:

- `acc-system-backend`
- `acc-system-frontend`
- `acc-system-nginx`

Create the GitHub Actions OIDC provider and one IAM role using the templates in `deploy/aws/`. Replace `<AWS_ACCOUNT_ID>` before applying them. The trust-policy template includes the immutable GitHub owner and repository IDs for these two repositories.

Configure both GitHub repositories with the secret `AWS_ROLE_ARN` and variable `AWS_REGION=ap-southeast-1`. Configure repository-name variables as follows:

| GitHub repository | Variable | Value |
| --- | --- | --- |
| backend | `ECR_BACKEND_REPOSITORY` | `acc-system-backend` |
| backend | `ECR_NGINX_REPOSITORY` | `acc-system-nginx` |
| frontend | `ECR_FRONTEND_REPOSITORY` | `acc-system-frontend` |

For the current Lightsail server, send a short-lived ECR login token from an SSO-authenticated workstation before pulling images:

```bash
aws ecr get-login-password --profile acc-system --region ap-southeast-1 \
  | ssh ubuntu@13.213.135.223 \
      'docker login --username AWS --password-stdin <AWS_ACCOUNT_ID>.dkr.ecr.ap-southeast-1.amazonaws.com'
```

The ECR token is temporary. Do not install permanent AWS access keys or put them in `.env.prod`.

## Local or staging HTTP

Build the three application images first. The frontend command runs in its own repository.

```bash
docker build --build-arg APP_VERSION=local -t acc-system-backend:local ..
docker build -t acc-system-nginx:local nginx
docker build --build-arg APP_VERSION=local -t acc-system-frontend:local ../../acc-system-frontend
cp .env.example .env.prod
docker compose --env-file .env.prod -f compose.yml up -d
docker compose --env-file .env.prod -f compose.yml --profile tools run --rm bootstrap
```

Run commands from the `deploy/` directory. Set `JWT_SECRET` to a random value (for example, `openssl rand -hex 32`) and set the bootstrap administrator fields before the first bootstrap run. For production, replace the three local image values with immutable ECR SHA tags and replace all example passwords.

Set `FRONTEND_ORIGIN` to the exact browser origin, including the scheme and any non-default port. `http://localhost` is for local acceptance only; visiting through a different host without changing this value prevents refresh and logout.

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
