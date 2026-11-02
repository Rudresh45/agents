# Kestra stack

The Docker stack pins **Kestra 1.3.45**. The flows use `ForEach` and
flow-level `pluginDefaults`, which are 1.x features. Kestra 2.0 renames
`ForEach` to `Loop` and removes `pluginDefaults` from Open Source, so do not
upgrade the image without changing the flows.

## Files

| Path | Purpose |
| --- | --- |
| `docker-compose.yml` | Backend, frontend, Kestra and Kestra PostgreSQL on one private Docker network. |
| `kestra/flows/` | Mounted read-only at `/app/flows` and loaded at startup with `--flow-path`. |
| `kestra/.env_encoded` | Generated base64 `SECRET_*` values. Gitignored. |
| `kestra/make-secrets.ps1` | Reads `GEMINI_API_KEY` and `INTERNAL_SERVICE_TOKEN` and writes `.env_encoded`. |
| `kestra/flows/system/hr_retention.yml` | Daily purge of old terminal executions, task outputs and logs. |
| `kestra/flows/hr_intake.yml` | Scheduled inbox classification, candidate creation and HR-manager digest. |
| `kestra/flows/hr_screening_and_decisions.yml` | Screening, drafting, HR approval `Pause`, approved send and CEO report. |
| `kestra/flows/fragments/hr_manager_screening.yml` | Phase 2 proof fragment kept for reference. |

## First run

1. Copy the root environment template and fill in the infrastructure secrets:

   ```powershell
   Copy-Item .env.example .env
   ```

   Generate values with:

   ```powershell
   python -c "import secrets; print(secrets.token_urlsafe(48))"
   python -c "import base64, os; print(base64.b64encode(os.urandom(32)).decode())"
   ```

   Put the first value in `INTERNAL_SERVICE_TOKEN`, the second in
   `KESTRA_ENCRYPTION_SECRET_KEY`. Set a strong `POSTGRES_PASSWORD`,
   `KESTRA_BASIC_AUTH_USERNAME` (a valid email) and
   `KESTRA_BASIC_AUTH_PASSWORD` (at least 8 characters, with an uppercase
   letter and a number).

2. Ensure `backend/.env` has a real `GEMINI_API_KEY`. The backend also needs
   it for the old `USE_KESTRA_LLM=false` path. The secret generator reads the
   root `.env` first, then falls back to `backend/.env`.

3. Generate Kestra's base64 secrets:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\kestra\make-secrets.ps1
   ```

   This writes `kestra/.env_encoded` with `SECRET_GEMINI_API_KEY` and
   `SECRET_INTERNAL_SERVICE_TOKEN`. Do not put either value in a flow file.
   Flow YAML only uses `{{ secret('GEMINI_API_KEY') }}` and
   `{{ secret('INTERNAL_SERVICE_TOKEN') }}`.

4. Start the stack:

   ```powershell
   docker compose up -d --build
   docker compose ps
   docker compose logs -f kestra backend
   ```

5. Open Kestra at <http://127.0.0.1:8080> and log in with the basic-auth
   credentials. The backend is at <http://127.0.0.1:8000>.

Kestra OSS 1.3 uses HTTP Basic auth for the UI and API. The backend is given
the same credentials as `KESTRA_API_USERNAME` and `KESTRA_API_PASSWORD`, so
`KestraClient` authenticates with Basic auth. `KESTRA_API_TOKEN` remains
available for Enterprise deployments that issue API tokens; Basic auth wins
when both are configured.

## Loading and reloading flows

Kestra loads every YAML file under the mounted `/app/flows` directory at
startup because the command includes `--flow-path /app/flows`. These flows
should appear in the Kestra UI:

- `hr.recruitment.hr_intake` (schedule trigger, every 15 minutes)
- `hr.recruitment.hr_screening_and_decisions` (started by the backend)
- `hr.recruitment.fragments.hr_manager_screening` (Phase 2 proof fragment)
- `system.hr_retention`

After editing a flow on disk, restart Kestra so the startup loader runs again:

```powershell
docker compose restart kestra
```

The full flows are assembled, but keep `USE_KESTRA_LLM=false` until Phase 6
validation is complete. The backend then still uses the old LangGraph path.

### Flow contracts

Both full flows call only `/internal/*` routes with
`Authorization: Bearer {{ secret('INTERNAL_SERVICE_TOKEN') }}`.

| Flow | Steps |
| --- | --- |
| `hr_intake` | `inbox/prepare` → AI classify (alias and AI-matched lists) → `inbox/apply` → `inbox/acknowledge` → AI reply classification → `forward/prepare` → AI summaries → `forward/apply` (per item and one finalize digest). |
| `hr_screening_and_decisions` | `screen/prepare` → AI scoring → `screen/apply` → `draft/prepare` → AI drafting → `draft/apply` → `Pause wait_for_hr_approval` → `send/approved` → `ceo-report/prepare` → AI summaries → `ceo-report/apply`. |

Every apply request carries the `prompt_version` returned by its prepare
step, so each AI decision is auditable. The backend owns all data, status,
approval and sending rules; Kestra only orchestrates and calls Gemini.

Raw sender addresses and attachments never enter Kestra. The intake prepare
step returns an opaque `email_key` and redacted prompts; the backend keeps the
raw email in a short-lived staging file until apply completes.

## Retention

`system.hr_retention` runs daily at 03:00 UTC with a 7-day window. It:

- purges terminal executions (`SUCCESS`, `WARNING`, `FAILED`, `KILLED`,
  `CANCELLED`) in the `hr.recruitment` namespace prefix, including task
  outputs, metrics and stored files;
- purges logs in the same namespace prefix;
- deliberately excludes `PAUSED` executions so an approval is never deleted
  while it is still waiting for HR.

Kestra execution outputs can contain sanitised prompts and resume-derived
text. Keep this retention short; do not rely on Kestra defaults.

## Network and exposure

Kestra reaches the backend at `http://backend:8000` on the `hr_internal`
network. Kestra's UI/API and the backend are published only on
`127.0.0.1`; PostgreSQL has no published port. `/internal/*` routes require
`Authorization: Bearer <INTERNAL_SERVICE_TOKEN>` and are not part of the
public OpenAPI schema. For a production deployment, remove the backend host
port mapping and put a reverse proxy in front of only the public routes.

## Pause rule

The approval `Pause` in `hr_screening_and_decisions` uses
`pauseDuration: P7D` with `behavior: CANCEL`. Kestra's default behaviour is
`RESUME`, which would send email without a fresh HR approval, so never remove
`behavior: CANCEL`. Seven days matches the retention window: a stale paused run
becomes terminal and is then purged instead of keeping resume-derived outputs
indefinitely.

The backend `POST /api/v1/workflow/approve` route resumes the execution with
`approved=true` and `approved_by=<user email>`. The flow's `approved_gate`
re-checks `approved == true` before `send/approved`; the backend also rejects
any unapproved draft with HTTP 409 (R6).

## Live validation status

Static YAML and Compose configuration can be checked without a daemon. Live
Kestra/Gemini validation requires Docker Desktop to be running and the
`kestra/.env_encoded` secrets to exist. Until that live check passes, keep
`USE_KESTRA_LLM=false` and treat the Kestra path as unproven.

The exact plugin-ai patch version bundled with the Kestra `v1.3.45` image was
not confirmed without pulling it. Task type names were verified against
plugin-ai `v1.16.0` documentation. `onResume` uses `BOOLEAN`; if the bundled
plugin rejects that spelling, use `BOOL`.
