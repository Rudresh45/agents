# Manual Dry-Run Plan: Kestra Pause, Approval, & Resume Flow

This plan walks through manually testing the Kestra orchestration and human approval flow end-to-end, validating both the rejection branch (`approved = false`) and the approval branch (`approved = true`).

---

## Prerequisites

1. **Start the Stack**:
   ```powershell
   cd "E:\hr agents"
   docker compose up -d
   ```
   - Kestra Web UI: <http://127.0.0.1:8080>
   - Backend API: <http://127.0.0.1:8000>
   - Backend Docs: <http://127.0.0.1:8000/docs>

2. **Verify Kestra Secrets**:
   Ensure `GEMINI_API_KEY` and `INTERNAL_SERVICE_TOKEN` are populated in Kestra (via `.\kestra\make-secrets.ps1` or the Kestra UI under **Administration > Secrets**).

3. **Log in as HR Admin**:
   ```powershell
   $base = "http://127.0.0.1:8000"
   $login = Invoke-RestMethod -Method Post -Uri "$base/api/v1/auth/login" `
     -ContentType "application/json" `
     -Body (@{ email = "hr@acme-hr.com"; password = "choose-a-strong-password" } | ConvertTo-Json)
   $headers = @{ Authorization = "Bearer $($login.access_token)" }
   ```

4. **Ensure an Open Job with Candidates**:
   Ensure an open job exists (`$jobId`) and candidates are in `forwarded` status ready for screening.

---

## Test Scenario A: Human Decline (`approved = false`)

### Step A.1: Start the Workflow Run
```powershell
$run = Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflow/run" `
  -Headers $headers -ContentType "application/json" `
  -Body (@{ job_id = $jobId } | ConvertTo-Json)

$runId = $run.run_id
$execId = $run.kestra_execution_id
Write-Host "Started Run: $runId with Kestra Execution: $execId"
```

### Step A.2: Confirm the Workflow Pauses in Kestra
1. Open Kestra UI:
   `http://127.0.0.1:8080/ui/executions/hr.recruitment/hr_screening_and_decisions`
2. Click on Execution `$execId`.
3. In the **Gantt** or **Topology** view, observe tasks executing:
   - `screen_prepare` (HTTP 200)
   - `score_each` (concurrency 1, Gemini ChatCompletion)
   - `screen_apply_finalize` (emails manager shortlist)
   - `draft_prepare` (HTTP 200)
   - `draft_each` (concurrency 1, Gemini ChatCompletion)
   - `wait_for_hr_approval` reaches **PAUSED** state.
4. Verify backend status:
   ```powershell
   $status = Invoke-RestMethod -Uri "$base/api/v1/workflow/status/$runId" -Headers $headers
   $status.status        # Must be "waiting_approval"
   $status.kestra_state  # Must be "PAUSED"
   ```

### Step A.3: Resume with `approved = false`
In the Kestra execution screen:
1. Click the **Resume** button on the `wait_for_hr_approval` task.
2. In the modal:
   - Leave `approved` unchecked / `false`.
   - Set `approved_by` to your name (e.g. `hr-reviewer`).
3. Click **Submit**.

### Step A.4: Verify Decline Invariants
1. In Kestra Topology:
   - Task `approved_gate` evaluates condition `outputs.wait_for_hr_approval.onResume.approved == true`.
   - Because `approved` is false, it branches to `else: log_declined`.
   - Log output reads: *"HR did not approve this run; no candidate email or CEO report was sent."*
   - Execution finishes with state **SUCCESS**.
2. In the Backend:
   - Check `GET $base/api/v1/drafts`: All drafts remain `approved: false` and `sent_at: null`.
   - Check Candidate status: Candidates remain in `shortlisted` or `not_shortlisted` (NEVER moved to `accepted`).
   - Check `GET $base/api/v1/reports`: Zero CEO reports were created or mailed.

---

## Test Scenario B: Human Approval (`approved = true`)

### Step B.1: Start a New Screening Run
```powershell
$run = Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflow/run" `
  -Headers $headers -ContentType "application/json" `
  -Body (@{ job_id = $jobId; force = $true } | ConvertTo-Json)
$runId = $run.run_id
$execId = $run.kestra_execution_id
```

### Step B.2: Confirm Pause State
Wait ~15–30 seconds until the flow reaches the `wait_for_hr_approval` Pause task.

### Step B.3: Review and Approve Drafts in Backend
1. Fetch generated unapproved drafts:
   ```powershell
   $drafts = Invoke-RestMethod -Uri "$base/api/v1/drafts?run_id=$runId" -Headers $headers
   $inviteDraft = $drafts.items | Where-Object { $_.draft_type -eq "interview_invite" } | Select-Object -First 1
   ```
2. Approve the interview invite draft:
   ```powershell
   Invoke-RestMethod -Method Post -Uri "$base/api/v1/drafts/$($inviteDraft.id)/approve" `
     -Headers $headers -ContentType "application/json"
   ```
3. Trigger the workflow approval resumption:
   ```powershell
   Invoke-RestMethod -Method Post -Uri "$base/api/v1/workflow/approve" `
     -Headers $headers -ContentType "application/json" `
     -Body (@{ run_id = $runId; draft_ids = @($inviteDraft.id) } | ConvertTo-Json)
   ```

### Step B.4: Observe Resumption and Completion in Kestra
1. In Kestra Execution view:
   - `wait_for_hr_approval` resumes with `approved = true`.
   - `approved_gate` condition evaluates to `true` and runs the `then` branch:
     - `send_approved`: calls `/internal/hr-agent/send/approved` and sends the approved interview invitation.
     - `report_prepare`: gathers accepted candidates with PII stripped.
     - `ai_report_summary`: generates executive summaries.
     - `report_finalize`: compiles the Excel workbook and emails the CEO.
2. Execution state becomes **SUCCESS**.

### Step B.5: Verify Approval Invariants
1. **Candidate Status**: The approved candidate transitioned from `shortlisted` to `accepted`.
2. **Audit Log**: Audit entries exist for `draft.approved`, `candidate.status_changed`, and `ceo_report.created` with model name and `prompt_version`.
3. **CEO Report**: Contains strictly `accepted` candidates with scores and summaries, and zero contact details (no email, phone, age, gender, or photo columns).
4. **Idempotency**: Retrying `POST /internal/hr-agent/send/approved` returns HTTP 200 with `counts.skipped >= 1` and sends zero duplicate emails.

