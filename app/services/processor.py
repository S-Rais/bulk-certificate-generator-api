"""Job creation, background processing, retry and crash recovery."""
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..models import Certificate, CertStatus, Job, JobStatus
from ..schemas import JobCreate, ProgressOut, recipient_adapter
from . import pdf

log = logging.getLogger("certgen")


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _format_validation_error(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in e['loc']) or 'recipient'}: {e['msg'].removeprefix('Value error, ')}"
        for e in exc.errors()
    )


# ---------------------------------------------------------------- creation
def create_job(
    db: Session, payload: JobCreate, max_recipients: int, idempotency_key: str | None
) -> tuple[Job, bool]:
    """Persist a job and one Certificate row per submitted recipient.

    Invalid recipients are stored as FAILED rows (with the reason) instead of rejecting the
    request, so the client gets a complete, per-row report and valid rows still proceed.
    Returns (job, created). created=False means an idempotent replay of an earlier request.
    """
    if idempotency_key:
        existing = db.scalar(select(Job).where(Job.idempotency_key == idempotency_key))
        if existing:
            return existing, False

    if len(payload.recipients) > max_recipients:
        raise ValueError(f"too many recipients: {len(payload.recipients)} > limit {max_recipients}")

    job = Job(
        course_name=payload.course_name.strip(),
        issuer_name=payload.issuer_name.strip(),
        issue_date=payload.issue_date,
        idempotency_key=idempotency_key,
    )
    seen_emails: set[str] = set()
    valid = 0
    for idx, raw in enumerate(payload.recipients):
        cert = Certificate(row_index=idx, recipient_name=str(raw.get("name", ""))[:300])
        cert.recipient_email = str(raw.get("email"))[:320] if raw.get("email") else None
        try:
            rec = recipient_adapter.validate_python(raw)
            email_key = rec.email.lower() if rec.email else None
            if email_key and email_key in seen_emails:
                raise ValueError(f"duplicate email in this job: {rec.email}")
            if email_key:
                seen_emails.add(email_key)
            cert.recipient_name = rec.name
            cert.recipient_email = rec.email
            valid += 1
        except ValidationError as exc:
            cert.status, cert.error = CertStatus.FAILED, _format_validation_error(exc)
        except ValueError as exc:
            cert.status, cert.error = CertStatus.FAILED, str(exc)
        job.certificates.append(cert)

    if valid == 0:  # nothing to do: finish immediately
        job.status, job.completed_at = JobStatus.FAILED, _now()
    db.add(job)
    db.commit()
    return job, True


def get_progress(db: Session, job_id: str) -> ProgressOut:
    rows = dict(
        db.execute(
            select(Certificate.status, func.count()).where(Certificate.job_id == job_id).group_by(Certificate.status)
        ).all()
    )
    generated = rows.get(CertStatus.GENERATED, 0)
    failed = rows.get(CertStatus.FAILED, 0)
    pending = rows.get(CertStatus.PENDING, 0)
    total = generated + failed + pending
    pct = round((generated + failed) / total * 100, 1) if total else 100.0
    return ProgressOut(total=total, generated=generated, failed=failed, pending=pending, percent_complete=pct)


# -------------------------------------------------------------- processing
class JobRunner:
    """Runs jobs on a small thread pool inside the API process.

    State lives in the database, not in memory, so a restart loses nothing: `recover()` re-queues
    unfinished jobs and already-generated certificates are skipped (processing is idempotent).
    """

    def __init__(self, session_factory: sessionmaker, settings: Settings):
        self._sf = session_factory
        self._settings = settings
        self._pool = ThreadPoolExecutor(max_workers=settings.worker_threads, thread_name_prefix="certgen")
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def dispatch(self, job_id: str) -> None:
        if self._settings.eager:
            self.run_job(job_id)
        else:
            self._pool.submit(self.run_job, job_id)

    def recover(self) -> int:
        with self._sf() as db:
            ids = db.scalars(
                select(Job.id).where(Job.status.in_([JobStatus.PENDING, JobStatus.PROCESSING]))
            ).all()
        for jid in ids:
            self.dispatch(jid)
        return len(ids)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    def run_job(self, job_id: str) -> None:
        with self._lock:  # never process the same job twice concurrently
            if job_id in self._active:
                return
            self._active.add(job_id)
        try:
            self._run(job_id)
        except Exception:  # last-resort guard: a worker thread must never die silently
            log.exception("job %s crashed unexpectedly", job_id)
        finally:
            with self._lock:
                self._active.discard(job_id)

    def _run(self, job_id: str) -> None:
        with self._sf() as db:
            job = db.get(Job, job_id)
            if job is None:
                return
            job.status, job.started_at, job.completed_at = JobStatus.PROCESSING, job.started_at or _now(), None
            db.commit()
            cert_ids = db.scalars(
                select(Certificate.id)
                .where(Certificate.job_id == job_id, Certificate.status == CertStatus.PENDING)
                .order_by(Certificate.row_index)
            ).all()

        for cid in cert_ids:
            self._generate_one(job_id, cid)

        with self._sf() as db:
            job = db.get(Job, job_id)
            p = get_progress(db, job_id)
            if p.generated == 0:
                job.status = JobStatus.FAILED
            elif p.failed:
                job.status = JobStatus.COMPLETED_WITH_ERRORS
            else:
                job.status = JobStatus.COMPLETED
            job.completed_at = _now()
            db.commit()

    def _generate_one(self, job_id: str, cert_id: str) -> None:
        """Each certificate gets its own session + transaction: one failure can't poison the rest."""
        with self._sf() as db:
            cert = db.get(Certificate, cert_id)
            job = db.get(Job, job_id)
            if cert is None or job is None or cert.status != CertStatus.PENDING:
                return
            cert.attempts += 1
            try:
                rel = f"{job_id}/{cert.id}.pdf"
                pdf.render_certificate(
                    self._settings.storage_dir / rel,
                    recipient_name=cert.recipient_name,
                    course_name=job.course_name,
                    issuer_name=job.issuer_name,
                    issue_date=job.issue_date,
                    certificate_id=cert.id,
                    verification_code=cert.verification_code,
                    verification_url=f"{self._settings.public_base_url}/api/v1/verify/{cert.verification_code}",
                )
                cert.status, cert.file_path, cert.error, cert.generated_at = (
                    CertStatus.GENERATED, rel, None, _now(),
                )
            except Exception as exc:
                log.warning("certificate %s failed: %s", cert_id, exc)
                cert.status, cert.error = CertStatus.FAILED, f"generation error: {exc}"[:500]
            db.commit()


def reset_failed_for_retry(db: Session, job_id: str) -> int:
    """Re-queue certificates that failed *during generation* (attempts > 0).
    Validation failures (attempts == 0) are bad input and are not retryable."""
    certs = db.scalars(
        select(Certificate).where(
            Certificate.job_id == job_id, Certificate.status == CertStatus.FAILED, Certificate.attempts > 0
        )
    ).all()
    for c in certs:
        c.status, c.error = CertStatus.PENDING, None
    if certs:
        db.get(Job, job_id).status = JobStatus.PENDING
    db.commit()
    return len(certs)
