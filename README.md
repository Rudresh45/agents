# HR AI Agent System — Backend

Three-agent recruitment backend: an HR inbox agent, an HR-manager screening agent
and a CEO reporting agent. The workflow is currently coordinated by LangGraph;
the Kestra migration keeps that path behind `USE_KESTRA_LLM=false` until the
new orchestration is proven. Every candidate-facing email is blocked until HR
approves the draft.

## Safety rules baked into the code

- No candidate email is sent without `EmailDraft.approved = true` and an HR
  approval recorded on the workflow run (`R6`).
- Candidate status changes only happen through `CandidateService` and only along
  the transitions in `app/core/constants.py`.
- LLM prompts receive redacted text: names, emails and phone numbers are removed
  before any call; candidate identity comes from deterministic parsing (`R9`).
- The CEO report contains only accepted (approved) candidates and never includes
  email, phone, gender, age or photo columns.
- Audit rows are append-only; the database layer rejects updates and deletes.
- Exactly one workflow run can be active at a time (partial unique index plus a
  service check).
- `EMAIL_DRY_RUN=true` by default: SMTP is exercised but nothing leaves the
  machine.

## Requirements

- Windows PowerShell (commands below are PowerShell)
- Python 3.12+ (developed on 3.14)
- Docker Desktop for the Kestra Compose stack
- A Gemini API key for real LLM calls (automated tests use a fake LLM and need no
  network)
- An IMAP/SMTP account for a real inbox run (optional for tests; required for a
  live workflow)

## Quick start (if the venv and `.env` already exist)

```powershell
cd "E:\hr agents\backend"
..\.venv\Scripts\alembic.exe upgrade head
..\.venv\Scripts\uvicorn.exe app.main:app --reload --port 8000
```

In a second terminal:

```powershell
cd "E:\hr agents\backend"
..\.venv\Scripts\python.exe -m pytest -q
```

Then open <http://127.0.0.1:8000/docs>. Full setup and manual checks follow.

## 1. Run the backend locally

All commands start in the repository root (`E:\hr agents`).

### 1.1 Create the virtual environment and install dependencies

```powershell
cd "E:\hr agents"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
cd backend
pip install -r requirements.txt
```

If PowerShell blocks activation, either use the executables directly (as shown
in the rest of this guide) or run:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

### 1.2 Create `.env`

```powershell
cd "E:\hr agents\backend"
Copy-Item .env.example .env -Force
```

Edit `.env` and set at least:

```dotenv
APP_ENV=dev
JWT_SECRET=replace-with-a-random-string-at-least-32-characters
HR_ADMIN_EMAIL=hr@yourcompany.com
HR_ADMIN_PASSWORD=choose-a-strong-password
HR_MANAGER_EMAIL=manager@yourcompany.com
HR_MANAGER_PASSWORD=choose-a-strong-password
CEO_EMAIL=ceo@yourcompany.com
CEO_PASSWORD=choose-a-strong-password
GEMINI_API_KEY=your-gemini-key
EMAIL_DRY_RUN=true
```

For a real inbox run also set `IMAP_*`, `SMTP_*` and `CEO_REPORT_EMAIL`. Keep
`EMAIL_DRY_RUN=true` until you have verified the approval flow end to end.

### 1.3 Create or migrate the database

```powershell
..\.venv\Scripts\alembic.exe upgrade head
```

Alembic owns the schema in every environment; the app no longer calls
`create_all` at startup. To reset a local SQLite database, stop the server,
delete `backend\hr_agent.db` and `backend\checkpoints.sqlite`, then run the
migration again.

### 1.4 Seed the three accounts

Startup seeds accounts idempotently, but you can run it explicitly:

```powershell
..\.venv\Scripts\python.exe scripts\seed_users.py
```

Accounts come from `HR_ADMIN_*`, `HR_MANAGER_*` and `CEO_*` in `.env`.

### 1.5 Start the API

```powershell
..\.venv\Scripts\uvicorn.exe app.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

- API docs: <http://127.0.0.1:8000/docs>
- Health: <http://127.0.0.1:8000/health>

`GET /health` is public. Everything else is under `/api/v1` and needs a bearer
token from `POST /api/v1/auth/login`. The `/internal/*` routes are network-only
and require the separate `INTERNAL_SERVICE_TOKEN`.

Expected health response (values vary by configuration):

```json
{
  "status": "healthy",
  "checks": {
    "database": {"status": "ok", "latency_ms": 0.4},
    "imap": {"status": "not_configured"},
    "smtp": {"status": "dry_run"},
    "llm": {"status": "not_configured"}
  }
}
```

### 1.6 Run the Kestra stack with Docker Compose

The repository includes `docker-compose.yml` with the backend, a frontend
service, Kestra 1.3.45 and Kestra's PostgreSQL. Kestra loads flows from
`kestra/flows` at startup. Detailed setup, secret generation and retention
notes are in [`kestra/README.md`](kestra/README.md).

```powershell
cd "E:\hr agents"
Copy-Item .env.example .env
# Fill in POSTGRES_PASSWORD, KESTRA_BASIC_AUTH_*, KESTRA_ENCRYPTION_SECRET_KEY
# and INTERNAL_SERVICE_TOKEN in .env, then:
powershell -ExecutionPolicy Bypass -File .\kestra\make-secrets.ps1
docker compose up -d --build
```

Kestra is available only on <http://127.0.0.1:8080> and the backend on
<http://127.0.0.1:8000>. PostgreSQL has no published port. The two full flows
(`hr.recruitment.hr_intake` and
`hr.recruitment.hr_screening_and_decisions`) are assembled, but keep
`USE_KESTRA_LLM=false` until Phase 6 validation proves the Kestra path.

### 1.7 Internal Kestra endpoints

Kestra calls these routes on the private Docker network. They require
`Authorization: Bearer <INTERNAL_SERVICE_TOKEN>`, use constant-time comparison,
and are hidden from the public OpenAPI schema:

| Route | Purpose |
| --- | --- |
| `POST /internal/hr-manager/screen/prepare` / `apply` | Sanitised scoring input and validated scores. |
| `POST /internal/hr-agent/inbox/prepare` | Fetch unread mail and return redacted classification prompts plus opaque `email_key`s. |
| `POST /internal/hr-agent/inbox/apply` | Apply one application classification/job match and create the candidate once. |
| `POST /internal/hr-agent/inbox/reply` | Apply one candidate reply classification. |
| `POST /internal/hr-agent/inbox/acknowledge` | Create the acknowledgement or resubmission draft once. |
| `POST /internal/hr-agent/forward/prepare` / `apply` | Redacted resume summaries and one candidate digest. |
| `POST /internal/hr-agent/draft/prepare` / `apply` | Decision-email prompts and unapproved drafts. |
| `POST /internal/hr-agent/send/approved` | Send only approved drafts; rejects any unapproved draft with 409. |
| `POST /internal/ceo-report/prepare` / `apply` | Redacted CEO summaries and one report per run. |
| `POST /internal/alerts` | Deduplicated flow-failure alert from a Kestra error handler. |

Every prepare/apply pair is idempotent. Staging files live under
`UPLOAD_DIR/.staging/inbox`; outbound markers live under `UPLOAD_DIR/.markers`.

The `frontend` service currently serves a placeholder because no React source
was present in this workspace; replace `frontend/dist` with the real build
output when available.

## 2. Test the backend manually

These examples use PowerShell and the seeded accounts. Start the server first in
another terminal.

### 2.1 Log in and store a token

```powershell
$base = "http://127.0.0.1:8000"

$login = Invoke-RestMethod -Method Post `
  -Uri "$base/api/v1/auth/login" `
  -ContentType "application/json" `
  -Body (@{ email = "hr@yourcompany.com"; password = "choose-a-strong-password" } | ConvertTo-Json)

$headers = @{ Authorization = "Bearer $($login.access_token)" }
$login.user
```

You should see the user with role `hr`. Log in with the manager and CEO accounts
to check role-based access (for example, the CEO must receive `403` from
`/api/v1/candidates`).

### 2.2 Check the role matrix

```powershell
# Unauthenticated -> 401
try { Invoke-RestMethod "$base/api/v1/jobs" } catch { $_.Exception.Response.StatusCode.value__ }

# HR cannot create a job -> 403
try {
  Invoke-RestMethod -Method Post -Uri "$base/api/v1/jobs" -Headers $headers `
    -ContentType "application/json" `
    -Body (@{ title = "Test"; required_skills = @("python") } | ConvertTo-Json)
} catch { $_.Exception.Response.StatusCode.value__ }
```

Log in as the manager and create a real job:

```powershell
$managerLogin = Invoke-RestMethod -Method Post `
  -Uri "$base/api/v1/auth/login" `
  -ContentType "application/json" `
  -Body (@{ email = "manager@yourcompany.com"; password = "choose-a-strong-password" } | ConvertTo-Json)
$managerHeaders = @{ Authorization = "Bearer $($managerLogin.access_token)" }

$job = Invoke-RestMethod -Method Post -Uri "$base/api/v1/jobs" -Headers $managerHeaders `
  -ContentType "application/json" `
  -Body (@{
    title = "Backend Engineer"
    description = "Build Python services."
    required_skills = @("python", "sql")
    preferred_skills = @("fastapi")
    min_experience_years = 2
    min_score = 60
    location = "Bengaluru"
  } | ConvertTo-Json)

$job
```

### 2.3 Run the workflow (live inbox mode)

A live run needs valid IMAP and Gemini credentials. Send a test application email
with a PDF/DOCX resume to the configured inbox, then:

```powershell
$run = Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflow/run" `
  -Headers $headers -ContentType "application/json" -Body '{"force":false}'
$run

$status = Invoke-RestMethod -Uri "$base/api/v1/workflow/status/$($run.run_id)" -Headers $headers
$status | ConvertTo-Json -Depth 6
```

Expected pause state:

```json
{
  "status": "waiting_approval",
  "stage": "draft_decisions",
  "pending_approval": {"drafts": 2, "approved": 0}
}
```

At this point **no candidate email has been sent**. Verify that in the dry-run
mailer output/logs.

### 2.4 Review, edit and approve drafts

```powershell
$drafts = Invoke-RestMethod -Uri "$base/api/v1/drafts?run_id=$($run.run_id)" -Headers $headers
$drafts.items | Format-Table id, draft_type, subject, approved, sent_at

$draftId = $drafts.items[0].id

# Optional edit before approval (editing an approved draft resets approval)
Invoke-RestMethod -Method Patch -Uri "$base/api/v1/drafts/$draftId" -Headers $headers `
  -ContentType "application/json" `
  -Body (@{ subject = "Interview invitation - Backend Engineer" } | ConvertTo-Json)

# Approve the draft itself
Invoke-RestMethod -Method Post -Uri "$base/api/v1/drafts/$draftId/approve" -Headers $headers

# Approve the run and resume the graph
$approve = @{ run_id = $run.run_id; draft_ids = @($draftId) } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflow/approve" -Headers $headers `
  -ContentType "application/json" -Body $approve

$final = Invoke-RestMethod -Uri "$base/api/v1/workflow/status/$($run.run_id)" -Headers $headers
$final | ConvertTo-Json -Depth 6
```

The run should move to `finished`. Only approved drafts are sent. The CEO report
is generated after sending; check:

```powershell
$reports = Invoke-RestMethod -Uri "$base/api/v1/reports/ceo" -Headers $headers
$reports.items

$reportId = $reports.items[0].id
Invoke-RestMethod -Uri "$base/api/v1/reports/ceo/$reportId" -Headers $headers | ConvertTo-Json -Depth 5

# Download Excel or PDF
Invoke-WebRequest -Uri "$base/api/v1/reports/ceo/$reportId/download?format=xlsx" `
  -Headers $headers -OutFile "ceo_report.xlsx"
Invoke-WebRequest -Uri "$base/api/v1/reports/ceo/$reportId/download?format=pdf" `
  -Headers $headers -OutFile "ceo_report.pdf"
```

### 2.5 Test without a real inbox

The reliable no-network path is the automated workflow test, which uses a fake
IMAP client, fake LLM and capturing mailer:

```powershell
..\.venv\Scripts\python.exe -m pytest tests/integration/test_workflow_api.py -q
```

It exercises: start run -> pause for approval -> assert no candidate email ->
approve -> send -> finish.

### 2.6 Test privacy and deletion

```powershell
# Queue deletion for a candidate
Invoke-RestMethod -Method Delete -Uri "$base/api/v1/candidates/<candidate-id>" -Headers $headers

# List and complete data requests
$requests = Invoke-RestMethod -Uri "$base/api/v1/privacy/requests" -Headers $headers
$requests

Invoke-RestMethod -Method Post `
  -Uri "$base/api/v1/privacy/requests/$($requests[0].id)/complete" -Headers $headers

# Retention sweep (anonymises candidates past RETENTION_DAYS)
Invoke-RestMethod -Method Post -Uri "$base/api/v1/privacy/sweep" -Headers $headers
```

## 3. Run the automated tests

Run from `E:\hr agents\backend`.

### 3.1 Full suite

```powershell
..\.venv\Scripts\python.exe -m pytest -q
```

Expected: all tests pass (currently **284**). The suite uses in-memory SQLite, a
deterministic fake LLM, a fake IMAP client and a capturing mailer, so it needs no
network, no Gemini key and no mailbox.

### 3.2 Test one layer at a time

```powershell
..\.venv\Scripts\python.exe -m pytest tests/unit -q
..\.venv\Scripts\python.exe -m pytest tests/services -q
..\.venv\Scripts\python.exe -m pytest tests/agents -q
..\.venv\Scripts\python.exe -m pytest tests/graph -q
..\.venv\Scripts\python.exe -m pytest tests/integration -q
..\.venv\Scripts\python.exe -m pytest tests/architecture -q
```

What each layer checks:

| Suite | What it proves |
| --- | --- |
| `tests/unit` | Status transition matrix, JWT tamper/expiry, password hashing, storage path-traversal guards, scheduler, PDF/Excel helpers, retry and redaction. |
| `tests/services` | Audit immutability, candidate lifecycle, duplicate detection, draft approval/supersede/send guards, workflow approval idempotency, privacy export/deletion. |
| `tests/agents` | Inbox parsing/dedupe, forwarding digest, screening and scoring, prompt injection resistance, draft generation, send gate, CEO report PII exclusion. |
| `tests/graph` | Pause before send, approve and resume, no-approval rejection, failed-node resume without refetch, cancel path, SQLite checkpoint persistence. |
| `tests/integration` | 401/403 role matrix, validation/404/pagination, full API workflow with an inline runner, draft/reply handling, audit CSV, privacy deletion, report Excel/PDF download. |
| `tests/architecture` | Layer contracts: routes do not import ORM models, agents never import other agents or the DB layer, only `llm_factory` imports Gemini. |

### 3.3 Lint and architecture checks

```powershell
..\.venv\Scripts\ruff.exe check app tests scripts
..\.venv\Scripts\lint-imports.exe
```

Expected: `All checks passed!` and `Contracts: 6 kept, 0 broken.`

### 3.4 Coverage

```powershell
..\.venv\Scripts\python.exe -m pytest --cov=app --cov-report=term-missing
```

Current overall coverage is about 90%; service modules are mostly 85–97%.

### 3.5 Migration smoke test

```powershell
$env:DATABASE_URL = "sqlite:///E:/hr agents/backend/_migrate_test.db"
..\.venv\Scripts\alembic.exe upgrade head
..\.venv\Scripts\alembic.exe downgrade base
Remove-Item "E:\hr agents\backend\_migrate_test.db"
$env:DATABASE_URL = $null
```

Alembic prints progress to stderr, so PowerShell may show it as a red
`NativeCommandError` even when the command succeeded. Check the final
`Running upgrade/downgrade` lines and the exit state.

### 3.6 API boot smoke test

```powershell
..\.venv\Scripts\uvicorn.exe app.main:app --host 127.0.0.1 --port 8000
```

In another terminal:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
(Invoke-WebRequest http://127.0.0.1:8000/docs -UseBasicParsing).StatusCode
```

Expected: health returns `200` with a JSON status, and `/docs` returns `200`.

## 4. What a successful test looks like

- `pytest` finishes with all tests passing and no network calls.
- `ruff` and `lint-imports` are clean.
- `/health` returns `200`; `/docs` loads.
- A workflow run pauses at `waiting_approval` with `approved: 0`.
- No candidate address appears in the mailer outbox before approval.
- After approval, the run finishes and only approved drafts are sent.
- The CEO report download contains no candidate email, phone, gender or age
  columns.

## 5. Troubleshooting

| Symptom | Fix |
| --- | --- |
| `uvicorn` not found | Use `..\.venv\Scripts\uvicorn.exe` from `backend`, or activate the venv first. |
| `ModuleNotFoundError: app` | Run commands from `E:\hr agents\backend`, not from the repository root. |
| `401 unauthorized` | Log in again and pass `Authorization: Bearer <token>`; tokens expire after `JWT_EXPIRE_MINUTES`. |
| `403 forbidden` | Use the right role: job create/update/close = `hr_manager`; approve/cancel = `hr`; audit/logs = `hr`; reports = `hr` or `ceo`. |
| Workflow run fails immediately | A live run needs `GEMINI_API_KEY` and valid `IMAP_*` credentials. Use the fake-LLM test suite for a no-network run. |
| No candidates found | The inbox had no unread application emails, or the resume was unreadable (`RESUME_MIN_CHARS`). |
| `409 run_already_active` | A run is active. Finish/cancel it, or call `POST /workflow/run` with `{"force": true}`. |
| `409 approval_required` | Approve at least one draft with `POST /drafts/{id}/approve` before `POST /workflow/approve`. |
| Database schema errors | Run `..\.venv\Scripts\alembic.exe upgrade head`; delete the local SQLite DB only if you intend to reset it. |
| Login returns `429` | Login rate limit is 5 attempts/minute/IP. Wait a minute; it is disabled when `APP_ENV=test`. |

## 6. Layout

```
backend/app
  agents/          hr_agent, hr_manager_agent, ceo_report_agent (+ prompt registry)
  api/             auth, workflow, jobs, candidates, drafts/replies, reports,
                   audit, privacy, logs, health
  graph/           LangGraph state, nodes, edges, runner, SQLite checkpointer
  services/        the only writers of candidates, drafts, audit, reports
  db/              SQLAlchemy models and session helpers
  utils/           LLM factory, redaction, IMAP/SMTP, storage, Excel, PDF
  workers/         optional dependency-free scheduler
backend/alembic    migrations
backend/scripts    seeding, manual agent runs, quality harnesses
backend/tests      unit, services, agents, graph, integration, architecture
```

Architecture rules are enforced by `import-linter` and
`tests/architecture/test_rules.py`: agents never import other agents or the DB
layer, routes do not import ORM models, and only `llm_factory` may import the
Gemini client.

## 7. Configuration notes

- `DATABASE_URL` defaults to SQLite; PostgreSQL works through SQLAlchemy.
- `CHECKPOINT_DB` stores LangGraph checkpoints (`checkpoints.sqlite`).
- `SCHEDULER_ENABLED=false` by default. When enabled, a background loop starts a
  run every `POLL_INTERVAL_MINUTES` if no run is active.
- Password hashing uses the standard-library `scrypt` implementation
  (`scrypt$n$r$p$salt$hash`) instead of Argon2, to avoid native build issues on
  Python 3.14.
- `RESUME_MIN_CHARS` controls when a resume is considered unreadable and the
  candidate is asked to resubmit.
- `RETENTION_DAYS` is enforced by `POST /api/v1/privacy/sweep`.
- `EMAIL_DRY_RUN=true` captures outbound mail instead of using SMTP.

## 8. Manual quality harnesses (need a real Gemini key)

```powershell
# Scoring agreement, false-reject rate and 3-run consistency
..\.venv\Scripts\python.exe scripts\evaluate_scoring.py `
  --job-id <job-uuid> --dataset labelled.json

# Swapped-name/gender score delta report
..\.venv\Scripts\python.exe scripts\bias_check.py `
  --job-id <job-uuid> --dataset bias_pairs.json

# Run one agent step manually
..\.venv\Scripts\python.exe scripts\run_agent.py inbox
..\.venv\Scripts\python.exe scripts\run_agent.py screen --run-id <uuid>
```
