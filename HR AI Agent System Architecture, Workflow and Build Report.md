# HR AI Agent System: Architecture, Workflow and Build Report

Prepared as the single source of truth for building the recruitment automation system. Stack: FastAPI, LangChain, LangGraph, Gemini API, React.

## 1. Purpose and scope

The system automates the repetitive part of hiring while keeping people in control of every decision.

- Read job application emails from the HR inbox.
- Forward new candidates to the HR manager.
- Score each candidate against the requirements of the job they applied for.
- Send HR an updated shortlist.
- After HR approves, send acceptance and rejection emails.

* Send the CEO a read-only report of the approved shortlist, with each candidate's score and skills.

Out of scope for the first version: interview scheduling, job portal integrations, and fully automatic decisions. These can be added later without changing the core design (see section 17).

## 2. System overview

```
 Candidates --email--> HR Gmail inbox
                            |
                            v
+------------------- BACKEND (FastAPI) -------------------+
|  API routes -> Services -> LangGraph orchestrator       |
|                                |                        |
|        +-----------------------+---------------+        |
|        v                       v               v        |
|    HR AGENT            HR MANAGER AGENT   CEO REPORT    |
|    inbox, forward,     scoring,            AGENT        |
|    drafts, sends       shortlist           report       |
|        +-----------------------+---------------+        |
|      Utils: LLM factory, IMAP, SMTP, parser, alerts     |
|                        |                                |
|             Database + uploads + audit log              |
+------------------------+--------------------------------+
        ^                                         |
        | REST + JWT                              v
 React dashboard                            CEO: read-only report
 (HR, HR manager)                           by email

 External services: Gemini API, Gmail, alert channel (email or Slack)
```

## 3. Frozen design rules

These rules do not change. If a new requirement conflicts with one, the new code is changed, not the rule.

| # | Rule |
| --- | --- |
| R1 | There are three agents, one per role (HR, HR manager, CEO). Each owns one area of work and never does another agent's job. |
| R2 | Agents never call each other. Only the orchestrator connects them. |
| R3 | Layer order is fixed: Routes, Services, Agents, Utils, Database. A lower layer never imports a higher one. |
| R4 | Routes contain no business logic. Agents never run raw queries. |
| R5 | All LLM calls go through one LLM factory and return structured (Pydantic) output. |
| R6 | No accept or reject email is sent without HR approval. |
| R7 | A candidate is scored only against their own job, and only on job-relevant data. |
| R8 | Every status change and decision is written to the audit log by the service layer. |
| R9 | Personal data is minimised, kept for a limited time, and can be deleted. |
| R10 | All secrets and settings come from `.env` through `config.py`. |
| R11 | The status flow is fixed. New statuses may be added, but existing transitions are never removed or reordered. |

## 4. Architecture layers

| Layer | Contains | Must not |
| --- | --- | --- |
| Frontend (React) | Pages, forms, tables, approval screens | Call Gemini or email directly |
| API routes | Validation, authentication, calling a service or the graph | Hold logic or queries |
| Services | Business rules, status transitions, duplicate checks, audit writes | Call the LLM or send mail directly |
| Orchestrator (LangGraph) | Order of steps, pause for approval, resume, retry | Hold business logic |
| Agents | One task each | Call other agents, write the audit log directly |
| Utils | LLM factory, IMAP client, SMTP mailer, resume parser, alerting | Know about candidates or jobs |
| Database | Tables, constraints, indexes | Hold logic |

## 5. The three agents

| Agent | Works for | Tasks | Reads | Writes | Uses LLM | Must never |
| --- | --- | --- | --- | --- | --- | --- |
| 1. HR agent | HR | Read the inbox and save candidates, match them to a job, recognise replies, forward new candidates to the HR manager, then draft accept and reject emails and send only the approved ones | Inbox, scored candidates, drafts | Candidates (`received`, `needs_review`, `needs_resubmission`, `forwarded`, `accepted`, `rejected`), reply records, drafts, email log | Yes: classify, extract, match job. Optional: summaries and drafts | Score anyone, or send an unapproved draft |
| 2. HR manager agent | HR manager | Score each forwarded candidate against the requirements of their own job, build the shortlist and send it to HR | `forwarded` candidates, job requirements | Score, reason, matched and missing skills, status `shortlisted` or `not_shortlisted`, Excel shortlist | Yes: scoring | Email candidates, or make the final decision |
| 3. CEO report agent | CEO | Build a read-only report of the approved shortlist (who is shortlisted, their skills and scores) and send it to the CEO | Approved candidates, scores, skills | CEO report record and file, email log | Optional: two-line candidate summaries | Change a candidate or a decision, or include contact details, age, gender or photo |

Each agent works for one person. Inside an agent, the steps stay separate modules, and agents still never call each other. Only the orchestrator connects them.

| Person | Role in the system | Receives |
| --- | --- | --- |
| HR | Runs the process, reviews the shortlist, edits drafts and approves the emails | The shortlist with scores and reasons, and the email drafts to approve |
| HR manager | Gives the job requirements and receives the forwarded candidates | A digest of new candidates with resumes |
| CEO | Read-only | A report of the approved shortlist: candidate, role, score, matched and missing skills, short summary |

## 6. End-to-end workflow

```
Inbox -> [HR agent: read, save] -> [HR agent: forward] -> HR manager gets digest
                                                                 |
                                              HR manager gives job requirements
                                                                 |
                                         [HR manager agent: score, shortlist]
                                                                 |
                                                     shortlist sent to HR
                                                                 |
                                                [HR agent: draft emails]
                                                                 |
                                                HR REVIEW GATE (graph pauses)
                                                                 |
                                            HR edits, overrides, approves
                                                                 |
                                                [HR agent: send emails]
                                                                 |
                                                     accepted / rejected
                                                                 |
                                       [CEO report agent: report to the CEO]
```

| Step | Actor | Action | Result |
| --- | --- | --- | --- |
| 1 | Scheduler or HR button | Starts a workflow run | A run record and a thread id |
| 2 | HR agent | Fetches unread mail, classifies, extracts, parses the resume, matches the job | Candidates `received`, or `needs_review` or `needs_resubmission`; replies saved as tasks |
| 3 | HR agent | Sends one digest email to the HR manager | Candidates `forwarded` |
| 4 | HR manager | Creates or confirms job requirements in the dashboard | Job is open |
| 5 | HR manager agent | Scores each candidate against their own job and builds the shortlist | `shortlisted` or `not_shortlisted`; Excel shortlist sent to HR |
| 6 | HR agent (draft) | Writes accept and reject drafts | Drafts with `approved = false` |
| 7 | Orchestrator | Pauses before sending | Run state `waiting_approval` |
| 8 | HR | Reviews scores and reasons, overrides with a written reason, edits drafts, approves | Drafts `approved = true`, audit entries |
| 9 | Orchestrator | Resumes the same run | Continues to the send step |
| 10 | HR agent (send) | Sends approved drafts only | `accepted` or `rejected`, email log |
| 11 | CEO report agent | Builds a read-only report of the approved shortlist and sends it to the CEO | Report record, email log, audit entry |
| 12 | System | Closes the run | Run `finished` |

Steps 2 and 3 can run on a schedule. Steps 5 to 10 are started manually by HR, and step 11 runs automatically once the sending step finishes.

## 7. Candidate status machine

```
               +--> needs_review ------(HR assigns job)---+
               +--> needs_resubmission --(new file)-------+
email --> received <-------------------------------------+
              |
              v
          forwarded
              | (Screening)
        +-----+------+
        v            v
   shortlisted   not_shortlisted
        +-----+------+
              v
     draft created (approved = false)
              |
       HR REVIEW GATE (mandatory)
              |
        +-----+------+
        v            v
    accepted      rejected

Side exits from any state: withdrawn, deleted or anonymised
```

Allowed transitions are enforced only in the service layer. Each transition writes to the audit log with the actor, old value, new value and reason. The CEO report is read-only and never changes a candidate's status.

## 8. Orchestrator logic (LangGraph)

```
read_emails -> forward_to_hr -> screen_candidates -> draft_decisions -> [PAUSE] -> send_decisions -> ceo_report -> END
 HR agent       HR agent        HR manager agent      HR agent                      HR agent         CEO report agent
     |                                |
     +-- no new candidates -> END     +-- no open job -> END
```

- **State:** run id, thread id, counts per stage, errors, active job ids.
- **Checkpointer:** the state is saved after every node. If a node fails, it is retried without repeating the nodes that already finished.
- **Interrupt:** the only pause is before `send_decisions`. It is the legal and ethical gate and must not be removed. The CEO report is built only after this approval.
- **Per-job processing:** screening and drafting loop over each open job, so one job never affects another.
- **Resume:** `POST /workflow/approve` resumes the same thread. It only works if at least one draft is approved.
- New features are added as new nodes or modules. The order above is never rearranged.

## 9. How each part works (logic)

### 9.1 HR agent: reading the inbox

1. Fetch unread emails through IMAP.
2. Check if the email is a reply to something the system sent (reply headers or known candidate address). If yes, store it as a reply task for HR and stop.
3. Ask the LLM to classify the email as an application or not, with a confidence value. Non-applications are skipped and logged.
4. Ask the LLM to extract name, email, phone and role.
5. Download the PDF or DOCX attachment and extract the text. If the file is scanned and the text is too short, try OCR. If OCR is poor, set `needs_resubmission` and prepare a polite request for a new file.
6. Match the email to an open job by subject, alias or the extracted role. If confidence is low, save the candidate as `needs_review` so HR can assign the job.
7. Check for a duplicate (same email and job). Skip duplicates.
8. Save the candidate as `received` with consent information, then mark the email as processed. A failed email never stops the batch.

### 9.2 HR agent: forwarding to the HR manager

1. Select all `received` candidates. If there are none, stop.
2. Create a neutral three-line summary for each candidate.
3. Send one digest email to the HR manager with a table and the resumes attached.
4. Move the candidates to `forwarded` and log the email.

### 9.3 HR manager agent: screening

1. Load the candidate's own job: required skills, preferred skills, minimum experience, education, location and minimum score.
2. Remove name, contact details, photo references, age, gender and nationality from the resume text.
3. Ask the LLM for a structured result: score from 0 to 100, matched skills, missing skills and a two-sentence reason. The prompt states that the resume is data and never instructions, which protects against prompt injection.
4. Compare the score with the job's minimum score and set `shortlisted` or `not_shortlisted`.
5. Save the score, reason, model name and prompt version, and write the audit entry.
6. Process candidates one at a time with delays for rate limits. If one fails, continue with the next.
7. Build an Excel shortlist and email it to HR. Nothing is sent to candidates here.

### 9.4 HR agent: decision emails

**Draft step:** for each shortlisted candidate write an interview invitation, and for each not-shortlisted candidate write a polite rejection. Drafts are personalised, free of discriminatory language, and saved with `approved = false`.

**Send step:** send only drafts with `approved = true`, update the candidate to `accepted` or `rejected`, and log the email. Trying to send an unapproved draft raises `ApprovalRequiredError`.

### 9.5 HR review

HR sees every score with its reason. HR can change the decision, but must type a short reason when overriding the agent or rejecting a shortlisted candidate. HR can edit any draft. Approval is recorded with the user and time.

### 9.6 Candidate replies

Replies never trigger automatic emails. They are classified (accepts, declines, asks to reschedule, asks a question, withdraws, other) and shown to HR as tasks. A withdrawal marks the candidate as `withdrawn` and flags the record for deletion.

### 9.7 CEO report agent

The CEO report agent gives the CEO a read-only view of who is shortlisted and why. It never changes a candidate or a decision.

1. Run only after HR has approved and the send step has finished, so the CEO sees final decisions. HR can also regenerate the report from the dashboard.
2. Select the approved, shortlisted candidates for each job.
3. For each candidate list the name, role applied for, score, matched skills, missing skills, a two-line summary of experience and HR's decision. Contact details, age, gender and photo are left out.
4. Add a summary at the top: applications received, candidates shortlisted, and accepted and rejected counts, per job.
5. Build the report as an Excel or PDF file and send it to the CEO email address from `.env`. Optionally show it on a read-only dashboard page.
6. Save the report record, log the email and write the audit entry. Dry-run mode applies, as for every other email.

## 10. Data model

| Table | Purpose | Key fields |
| --- | --- | --- |
| Job | Open positions | title, required and preferred skills, min experience, education, location, min score, status, email alias |
| Candidate | Applicant | job id, contact data, resume path and text, summary, score, reason, matched and missing skills, status, consent fields, retention date, anonymised flag |
| EmailDraft | Outgoing decision emails | candidate id, job id, type, subject, body, approved, approved by, sent at |
| CandidateReply | Incoming replies | candidate id, category, handled |
| EmailLog | Every send and receive attempt | direction, address, subject, status, error |
| AuditLog | Append-only history | actor, action, entity, old and new value, reason, model and prompt version, time |
| DataRequest | Access and deletion requests | candidate id, type, status |
| WorkflowRun | Run tracking | thread id, state, started and finished times |
| CeoReport | Reports sent to the CEO | job id, candidate ids, file path, sent to, created by, created at, sent at |

Constraints: unique (email, job id), indexes on status and email, and no update or delete path for the audit log in code.

## 11. API surface

| Group | Endpoints |
| --- | --- |
| Auth | `POST /login` |
| Workflow | `POST /workflow/run`, `GET /workflow/status/{id}`, `POST /workflow/approve` |
| Jobs | create, list, update, close |
| Candidates | list with filters, detail, update, assign job |
| Drafts | get, edit |
| Replies | list, mark handled |
| Audit | list, export CSV (read-only) |
| System | `GET /logs`, `GET /health` |
| Reports | POST /reports/ceo (generate and send the CEO report), GET /reports/ceo (list and download) |

All routes except `/login` and `/health` require a valid JWT. HR and the HR manager use the dashboard. A CEO account, if one is created, can only read reports.

## 12. Frontend pages

1. Login.
2. Job requirements form for the HR manager, with validation.
3. Candidates table with filters by status and job, score, reason, resume link and status badges.
4. Shortlist review: switch accept or reject, edit the draft, enter an override reason.
5. Run agents dashboard: status of each of the three agents, Run button, and Approve and Send behind a confirmation dialog.
6. CEO report page: preview the report, send or resend it to the CEO, and download it as Excel or PDF.
7. Replies inbox.
8. Email log and audit history, with CSV export.
9. A banner when any health check fails.

Every page has loading, empty and error states. The frontend only talks to the API.

## 13. Privacy, security and fairness

| Area | Design |
| --- | --- |
| Privacy | Strip name, contact details and photo references before text goes to Gemini. Use a paid tier or a plan that excludes training use for real candidate data. Keep a retention period, anonymise expired records, and support deletion of rows, files and drafts. Include a short privacy notice in the acknowledgement email. |
| Consent | Store whether consent was given, where it came from, and when. |
| Legal | The DPDP Act 2023 applies in India (consent, purpose limitation, retention, erasure). This design supports it, but a lawyer must review it. It is not a claim of compliance. |
| Security | JWT login, file type and size checks, filename sanitising, protection against path traversal, secrets only in `.env`, resume text treated as data. |
| Fairness | Name, gender, age, photo and nationality are excluded from scoring. A bias test with swapped names is run before use and repeated regularly. Scores are advice; HR decides. |
| Audit | Every decision records who, what, when, why, and which model and prompt version produced it. |

## 14. Reliability and alerts

- Every external call (LLM, IMAP, SMTP) has a timeout, retry with backoff, logging and error handling.
- A minimum delay between Gemini calls respects free-tier limits.
- One failed item never stops the batch.
- Alerts go to an email address, and optionally to Slack or Telegram, for IMAP, SMTP and LLM failures, a run that did not finish, a candidate stuck in a status too long, and an approval waiting too many days.
- The same alert is sent at most once per hour.
- `GET /health` checks the database, IMAP, SMTP and LLM separately.
- Dry-run mode for email lets you test without sending anything.

## 15. Technology and configuration

| Part | Choice |
| --- | --- |
| Language | Python 3.11 |
| API | FastAPI |
| Agents and workflow | LangChain, LangGraph |
| LLM | Gemini through `langchain-google-genai` (free tier for testing only) |
| Database | SQLite to start (SQLAlchemy 2.0), PostgreSQL later with Alembic |
| Email | IMAP to read, SMTP to send, Gmail app password for the MVP, OAuth later |
| Resume parsing | `pdfplumber`, `python-docx`, OCR as fallback |
| Reports | `openpyxl` for the Excel shortlist |
| Frontend | React with Vite, Tailwind, axios |
| Tests | pytest |

Configuration variables in `.env`: Gemini key and model name, LLM tier, IMAP host, user and password, SMTP host, port, user and password, reply-to address, HR manager email, CEO email, alert email and webhook, database URL, upload directory, retention periods, default minimum score, polling interval, dry-run flag, JWT secret, and the HR admin account.

## 16. Folder structure

```
hr-agent/
  backend/
    app/
      api/          routes (auth, workflow, jobs, candidates, drafts, replies, audit)
      core/         config, logging, exceptions, security
      db/           engine, session, models
      schemas/      API schemas and LLM output schemas (separate files)
      services/     candidate, job, draft, log, audit, privacy services
      agents/       hr_agent, hr_manager_agent, ceo_report_agent
      graph/        LangGraph state, nodes, workflow
      utils/        llm_factory, imap_client, mailer, resume_parser, alerts
      main.py
    uploads/
    tests/
    .env.example
    requirements.txt
  frontend/
    src/ (api, pages, components)
  README.md
```

## 17. Build plan

Build in this order. Test each step on its own before starting the next.

| Phase | Work | How to know it is done |
| --- | --- | --- |
| 1. Foundation | Scaffold, config, logging, exceptions, database, models, schemas | The app starts and the tables are created |
| 2. Utilities | LLM factory, resume parser, IMAP client, SMTP mailer (with dry run), alerting | Each utility works alone: one sample resume parsed, one email fetched, one test mail in dry run |
| 3. Services | Candidate, job, draft, log and audit services with status transition checks | Invalid transitions are rejected and every change creates an audit entry |
| 4. Agents | HR agent, HR manager agent, then CEO report agent, one at a time as standalone scripts | Each agent produces the expected database changes with the LLM and mail mocked, then with real calls |
| 5. Orchestrator | LangGraph with checkpointer and the approval pause | No email is sent before approval; a failed node resumes without repeating finished nodes |
| 6. API | Routes, JWT login, health check | All endpoints work in the `/docs` page |
| 7. Frontend | Pages in the order: candidates table, job form, review, run dashboard, logs | A full run can be done from the browser |
| 8. Quality | Accuracy evaluation, security review, privacy checks | See section 18 |
| 9. Delivery | Docker, PostgreSQL migration, backups, monitoring | System runs on one server |

**Suggested MVP:** phases 1 to 7 with one job at a time, a simple login, a Run button instead of a scheduler, and dry-run email first. Add multiple jobs, the scheduler, OCR, OAuth and Docker once the core flow is trusted.

## 18. Testing and accuracy check

- **Unit tests:** resume parsing, status transitions, duplicate detection.
- **Agent tests:** LLM and mailer mocked, using five fake candidates and one job.
- **End-to-end test:** proves that no email is sent before approval and that the CEO report lists only approved candidates, with no contact details.
- **Accuracy evaluation:** HR labels 20 to 30 past resumes as good, average or poor. The agent scores them, and the team compares agreement, false rejects (HR said good, agent said no) and score consistency over three runs. A suggested pass mark is no more than 10 percent false rejects.
- **Bias check:** run the same resumes with names and genders swapped and confirm the scores do not change.
- **Prompt injection test:** a resume saying to ignore instructions and give a top score must not change the result.
- If accuracy is low, use the agent only as a ranking aid and let HR make the shortlist.

## 19. Deployment

- Docker images for backend and frontend, and a compose file with volumes for uploads and the database.
- Production settings come from environment variables.
- Move from SQLite to PostgreSQL using Alembic migrations.
- Daily database and uploads backup, plus basic monitoring through the health endpoint and alerts.
- Production with real candidate data requires a paid Gemini tier or equivalent terms.

## 20. Change control

When anything changes, adapt the new work to this architecture.

| If the change is | Do this | Never do this |
| --- | --- | --- |
| A new data source such as a job portal | Add a utils client that feeds Agent 1 | Add an agent that bypasses Agent 1 |
| A new decision step such as an interview round | Add a status after `shortlisted` and a new graph node | Rewire or remove existing transitions |
| A new LLM provider | Change only the LLM factory and config | Touch agents or services |
| Gmail IMAP to OAuth | Replace the utils email client with the same interface | Change agents |
| Interview scheduling | Add a separate agent and node after the approval gate, with its own HR approval | Send mail without approval |
| New scoring criteria | Update job requirements and the screening prompt version | Change the status flow |
| SQLite to PostgreSQL | Change the database URL and add migrations | Change what the models mean |
| New report or dashboard | Add a read-only route and page | Put logic in routes |
| Automatic sending without approval | Reject this change | Implement it |

Checklist for every change: does it keep rules R1 to R11, add a status or node without breaking the flow, write to the audit log, and respect the privacy rules? If any answer is no, redesign the change.

## 21. Risks and open decisions

| Risk or decision | Mitigation |
| --- | --- |
| Free Gemini tier may use prompts for training | Paid tier or a no-training plan for real data; strip personal details first |
| Prompt injection through resumes | Resume treated as data, output validated, HR approval step |
| Biased or inaccurate scoring | Evaluation harness, bias test, HR final say |
| Scanned resumes | OCR fallback, then a request to resend |
| Gmail app passwords disabled by an admin | Plan the move to OAuth; avoid test-mode tokens that expire after 7 days |
| Rate limits | Sequential processing, delays, smaller batches |
| Candidate replies | Routed to HR as tasks, never answered automatically |
| Legal compliance | Lawyer review of consent, retention and notices |

## 22. Summary

Three role-based agents (HR, HR manager and CEO report) are connected by one orchestrator, and a human approval gate sits before any decision email leaves the system. Services own the business rules and the audit trail, utilities isolate every external service, and the status machine and guardrails stay fixed while features grow around them. Build it in the phase order above, test each part alone, and treat privacy, fairness and approval as requirements, not extras.
