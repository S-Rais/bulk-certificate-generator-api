# Bulk Certificate Generator API

A FastAPI backend that accepts **one request containing many recipients**, validates each row,
renders a PDF certificate per valid recipient in the background, and lets clients track progress
and retrieve the results (individually or as a ZIP).

**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2.0 · SQLite (any SQL DB via one env var) · ReportLab · pytest

## Highlights
- Bulk-first API: `POST /jobs` returns `202` instantly; work happens in a background pool
- Per-recipient validation and **failure isolation**: bad rows never block good ones
- Retry for transient generation failures; crash recovery after restart
- Idempotency-Key support, optional API-key auth, public **QR-code verification** endpoint
- ZIP download with a `manifest.csv` covering every row (success *and* failure)
- 26 tests, Dockerfile, interactive OpenAPI docs at `/docs`

## Setup
```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env                                   # optional; defaults work
```

## Run
```bash
uvicorn app.main:app --reload
# Swagger UI: http://localhost:8000/docs
```
Docker: `docker compose up --build`

## Test
```bash
pytest -q
```

## Usage
**1. Submit a job**
```bash
curl -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" -H "Idempotency-Key: event-2026-09-batch-1" \
  -d '{
    "course_name": "Advanced Python Bootcamp",
    "issuer_name": "Aereo Academy",
    "issue_date": "2026-09-30",
    "recipients": [
      {"name": "Syed Rais", "email": "rais@example.com"},
      {"name": "Jane Doe"},
      {"name": "", "email": "broken"}
    ]
  }'
```
→ `202` with `id`, `status: pending`, and `progress`.

**2. Check progress** - `GET /api/v1/jobs/{id}`
`status` is one of `pending | processing | completed | completed_with_errors | failed`;
`progress` has `total / generated / failed / pending / percent_complete`.

**3. See per-recipient results (and errors)**
`GET /api/v1/jobs/{id}/certificates?status=failed&limit=100&offset=0`

**4. Retrieve certificates**
- One PDF: `GET /api/v1/certificates/{certificate_id}/download`
- Everything: `GET /api/v1/jobs/{id}/download` → ZIP of PDFs + `manifest.csv`

**5. Retry generation failures** - `POST /api/v1/jobs/{id}/retry`

**6. Verify a certificate (public)** - `GET /api/v1/verify/{code}` (the URL in the QR code)

## Design decisions

| Decision | Choice | Why |
|---|---|---|
| Sync vs async | **Async**: 202 + background thread pool + polling | Bulk requests can take long; holding an HTTP connection open risks timeouts and gives no progress. |
| Queue tech | In-process `ThreadPoolExecutor`, state in the DB | Zero extra infrastructure for this scope. ReportLab is CPU-bound pure Python, so threads give concurrency for I/O and keep the API responsive; for CPU scale-out see "Scaling". |
| Validation | Request-level (422) for structure; **row-level** for recipients | A single typo in 5,000 rows shouldn't reject the batch. Invalid rows are stored as `failed` with a reason. |
| Failure isolation | One session + transaction per certificate | One exception can't roll back or abort its siblings. |
| Progress | Computed with `GROUP BY` from certificate rows | No counters to drift out of sync; always accurate. |
| Durability | Job/rows persisted *before* work starts; startup `recover()` re-queues unfinished jobs; already-generated rows are skipped | Server restart mid-job loses nothing; processing is idempotent. |
| Retry | Only generation failures (`attempts > 0`) | Validation failures are bad input; retrying can't fix them. |
| File writes | Render to `.tmp` then `os.replace` | No half-written PDFs after a crash. |
| Idempotency | `Idempotency-Key` header, unique DB column | Client/network retries of the POST can't create duplicate jobs. |
| Verification | Unguessable 96-bit code, public endpoint, no email exposed | Makes certificates verifiable and tamper-evident without leaking PII. |
| DB | SQLite (WAL) by default | Runs anywhere; swap `CERTGEN_DATABASE_URL` for PostgreSQL with no code change. |

**Duplicate rule:** the same email twice in one job fails the second row (likely a data-entry error).
**Name rule:** names must be renderable by the template's built-in Latin fonts; other scripts fail
that row with a clear message (see "Known limitations").

## Scaling (what I'd do next)
1. Replace the thread pool with a real queue (Celery/RQ + Redis) and run workers as separate processes. `JobRunner.dispatch` is the only seam to change.
2. Move PDFs to S3/object storage and return pre-signed URLs.
3. PostgreSQL + `SELECT ... FOR UPDATE SKIP LOCKED` for multi-worker claiming; Alembic migrations.
4. Webhook on job completion; per-client rate limits.

## Known limitations
- Built-in PDF fonts are Latin-only. Supporting Indic/CJK names means embedding a Unicode TTF (e.g. Noto) in `services/pdf.py` and relaxing the validator.
- In-process workers share the API's CPU; fine for thousands of certificates, not millions.
- Schema is created with `create_all` (no migrations yet).

## Project layout
```
app/
  main.py            app factory, lifespan (create tables, recover jobs), /health
  config.py          env-driven settings
  models.py          Job, Certificate (+ status enums)
  schemas.py         request/response models, per-recipient validator
  deps.py            DB session, API-key auth
  routers/jobs.py    all endpoints
  services/
    processor.py     create_job, JobRunner (background), retry, progress
    pdf.py           certificate template (ReportLab + QR)
tests/               26 tests
```
