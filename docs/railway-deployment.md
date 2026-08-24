# Railway cloud deployment

This runbook deploys ME Warehouse Control to Railway's Singapore region. It is
designed for a warehouse that has no always-on server and needs HTTPS access
from employee Android phones.

## Recommended starting shape

Use one Railway project with four services:

| Service | Exposure | Persistence |
| --- | --- | --- |
| `Admin` | Public HTTPS domain | Stateless |
| `API` | Railway private network only | `/data/evidence` volume |
| `Postgres` | Railway private network only | Managed database backups |
| `Redis` | Railway private network only | Queue/cache data |

The React admin nginx service is the only public entrypoint. It proxies
`/api/*` to the private API, so employee mobile apps and supervisors use one
HTTPS hostname. The API, PostgreSQL and Redis are not exposed publicly.

Railway's Pro plan is recommended for business use. As of August 2026 it has a
USD 20 monthly minimum that is applied to usage. Actual cost depends on RAM,
CPU, storage and network usage. Configure a compute hard limit and billing
alerts before production rollout.

## Account and project

1. Sign in to Railway with the GitHub account that owns
   `erzp005-creator/me-warehouse-control`.
2. Select the Pro workspace only after the monthly charge is approved.
3. Create an empty project named `ME Warehouse Control`.
4. Set the preferred region to **Southeast Asia Metal — Singapore**
   (`asia-southeast1-eqsg3a`).
5. Add Railway PostgreSQL and Redis database services.

## API service

Create an empty service named `API`, connect the GitHub repository's `main`
branch, and keep the repository root as the build root. Configure:

- Dockerfile path: `/api/Dockerfile`
- Pre-deploy command: `python scripts/bootstrap_cloud_db.py`
- Healthcheck path: `/api/health`
- Region: Singapore
- No public domain
- Volume: 5 GB mounted at `/data/evidence`
- Pilot worker count: `GUNICORN_WORKERS=2`

Set these service variables. Use Railway reference variables for database
services rather than copying credentials:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
CELERY_BROKER_URL=${{Redis.REDIS_URL}}
CELERY_RESULT_BACKEND=${{Redis.REDIS_URL}}
FLASK_ENV=production
TRUST_PROXY=true
API_BIND_HOST=127.0.0.1
EVIDENCE_STORAGE_DIR=/data/evidence
SENTRY_COMPANY_TIMEZONE=Asia/Kuala_Lumpur
GUNICORN_WORKERS=2
GUNICORN_TIMEOUT=180
```

Create sealed values for:

- `ADMIN_PASSWORD` — at least 12 characters; used only when the database is empty
- `JWT_SECRET` — 32 random bytes as hexadecimal
- `SENTRY_ENCRYPTION_KEY` — Fernet key
- `SENTRY_TOKEN_PEPPER` — 32 random bytes as hexadecimal
- `SENTRY_PUBSUB_HMAC_KEY` — 32 random bytes as hexadecimal

The bootstrap refuses to create a fresh system if `ADMIN_PASSWORD` is weak or
missing. It loads the canonical schema, creates one empty warehouse and the
`admin` account, and verifies migration 082. Later deploys detect the existing
schema and do not reset data.

## Admin service

Create another service named `Admin` from the same `main` branch:

- Root directory: `/admin`
- Dockerfile: detected automatically as `/admin/Dockerfile`
- Healthcheck path: `/`
- Region: Singapore
- Generate one Railway public HTTPS domain

Set:

```text
API_UPSTREAM_URL=http://${{API.RAILWAY_PRIVATE_DOMAIN}}:5000
API_UPSTREAM_HOST=${{API.RAILWAY_PRIVATE_DOMAIN}}
```

After Railway generates the Admin domain, set this API variable and redeploy
the API:

```text
CORS_ORIGINS=https://${{Admin.RAILWAY_PUBLIC_DOMAIN}}
```

## First production check

1. Open the Admin HTTPS domain and sign in as `admin` using the sealed
   `ADMIN_PASSWORD`.
2. Change the password after access is confirmed, then seal or remove the
   bootstrap variable.
3. Rename `WH-01` and create employee accounts with only their required work
   types.
4. Create a two-order test Pack Note and verify picker/packer scanning.
5. Create one receiving task, submit SKU quantities plus a photo, and review it
   from Work Control.
6. Confirm the evidence file is present under the API volume and create the
   first volume/database backup.

## Employee Android app

The production mobile build must use the Admin HTTPS domain because nginx
proxies the API securely:

```text
EXPO_PUBLIC_API_URL=https://<admin-domain>
```

Do not build the APK until the cloud URL and end-to-end test are stable. The
first EAS build also requires the company's Expo account because the upstream
project ownership was intentionally removed.

## Deferred services

The Work Control pilot does not require the Celery worker, snapshot keeper,
webhook dispatcher or connector publisher. Add a Celery worker when SiteGiant
or another asynchronous connector is enabled. This keeps the pilot simpler and
reduces idle cloud cost without changing task allocation, timing, receiving or
evidence flows.

## Backup minimum

- Enable automated Postgres and API-volume backups.
- Keep at least seven daily restore points during the pilot.
- Test one restore before relying on the system for disciplinary evidence.
- Never treat a photo stored outside `/data/evidence` as durable.
