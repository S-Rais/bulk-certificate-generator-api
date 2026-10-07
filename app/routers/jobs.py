import csv
import tempfile
import zipfile
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from ..config import Settings
from ..deps import app_settings, get_db, get_runner, require_api_key
from ..models import Certificate, CertStatus, Job, JobStatus
from ..schemas import CertificateOut, CertificatePage, JobCreate, JobOut, VerificationOut
from ..services import processor

router = APIRouter(prefix="/api/v1", tags=["certificates"])
secured = APIRouter(dependencies=[Depends(require_api_key)])


def _job_out(db: Session, job: Job) -> JobOut:
    base = f"/api/v1/jobs/{job.id}"
    return JobOut(
        id=job.id, status=job.status, course_name=job.course_name, issuer_name=job.issuer_name,
        issue_date=job.issue_date, created_at=job.created_at, started_at=job.started_at,
        completed_at=job.completed_at, progress=processor.get_progress(db, job.id),
        links={"self": base, "certificates": f"{base}/certificates", "download_zip": f"{base}/download"},
    )


def _cert_out(c: Certificate) -> CertificateOut:
    out = CertificateOut.model_validate(c)
    if c.status == CertStatus.GENERATED:
        out.download_url = f"/api/v1/certificates/{c.id}/download"
    return out


def _get_job_or_404(db: Session, job_id: str) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job


@secured.post("/jobs", response_model=JobOut, status_code=202, summary="Submit a bulk generation job")
def create_job(
    payload: JobCreate,
    response: Response,
    db: Session = Depends(get_db),
    runner=Depends(get_runner),
    settings: Settings = Depends(app_settings),
    idempotency_key: str | None = Header(default=None, max_length=100),
):
    """Returns 202 immediately; poll `GET /jobs/{id}`. Send an `Idempotency-Key` header to make
    retries of this POST safe (a repeat returns the original job with 200 instead of creating a new one)."""
    try:
        job, created = processor.create_job(db, payload, settings.max_recipients_per_job, idempotency_key)
    except ValueError as exc:
        raise HTTPException(413, str(exc))
    if not created:
        response.status_code = 200
    elif job.status == JobStatus.PENDING:
        runner.dispatch(job.id)
        db.refresh(job)
    return _job_out(db, job)


@secured.get("/jobs/{job_id}", response_model=JobOut, summary="Job status and progress")
def get_job(job_id: str, db: Session = Depends(get_db)):
    return _job_out(db, _get_job_or_404(db, job_id))


@secured.get("/jobs/{job_id}/certificates", response_model=CertificatePage, summary="Per-recipient results")
def list_certificates(
    job_id: str,
    status: CertStatus | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    _get_job_or_404(db, job_id)
    q = select(Certificate).where(Certificate.job_id == job_id)
    if status:
        q = q.where(Certificate.status == status)
    total = db.scalar(select(func.count()).select_from(q.subquery()))
    rows = db.scalars(q.order_by(Certificate.row_index).limit(limit).offset(offset)).all()
    return CertificatePage(total=total, limit=limit, offset=offset, items=[_cert_out(c) for c in rows])


@secured.post("/jobs/{job_id}/retry", response_model=JobOut, status_code=202, summary="Retry generation failures")
def retry_job(job_id: str, db: Session = Depends(get_db), runner=Depends(get_runner)):
    job = _get_job_or_404(db, job_id)
    if job.status in (JobStatus.PENDING, JobStatus.PROCESSING):
        raise HTTPException(409, "job is still running")
    if processor.reset_failed_for_retry(db, job_id) == 0:
        raise HTTPException(409, "nothing to retry (validation failures are not retryable)")
    runner.dispatch(job_id)
    db.refresh(job)
    return _job_out(db, job)


@secured.get("/jobs/{job_id}/download", summary="Download all generated certificates as a ZIP")
def download_zip(job_id: str, db: Session = Depends(get_db), settings: Settings = Depends(app_settings)):
    """ZIP contains every generated PDF plus manifest.csv listing ALL recipients with status/error."""
    _get_job_or_404(db, job_id)
    certs = db.scalars(select(Certificate).where(Certificate.job_id == job_id).order_by(Certificate.row_index)).all()
    if not any(c.status == CertStatus.GENERATED for c in certs):
        raise HTTPException(409, "no certificates have been generated yet")

    tmp = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
    tmp.close()
    with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_STORED) as zf:
        manifest = Path(tmp.name + ".csv")
        with manifest.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["row", "name", "email", "status", "file", "verification_code", "error"])
            for c in certs:
                fname = ""
                if c.status == CertStatus.GENERATED:
                    safe = "".join(ch if ch.isalnum() else "_" for ch in c.recipient_name)[:50]
                    fname = f"{c.row_index + 1:05d}_{safe}.pdf"
                    zf.write(settings.storage_dir / c.file_path, fname)
                w.writerow([c.row_index + 1, c.recipient_name, c.recipient_email or "", c.status.value,
                            fname, c.verification_code, c.error or ""])
        zf.write(manifest, "manifest.csv")
        manifest.unlink()
    return FileResponse(
        tmp.name, media_type="application/zip", filename=f"certificates_{job_id}.zip",
        background=BackgroundTask(lambda: Path(tmp.name).unlink(missing_ok=True)),
    )


@secured.get("/certificates/{cert_id}/download", summary="Download one certificate PDF")
def download_certificate(cert_id: str, db: Session = Depends(get_db), settings: Settings = Depends(app_settings)):
    cert = db.get(Certificate, cert_id)
    if not cert:
        raise HTTPException(404, "certificate not found")
    if cert.status != CertStatus.GENERATED:
        raise HTTPException(409, f"certificate is {cert.status.value}")
    path = settings.storage_dir / cert.file_path
    if not path.exists():
        raise HTTPException(410, "certificate file is missing from storage")
    return FileResponse(path, media_type="application/pdf", filename=f"certificate_{cert.id}.pdf")


@router.get("/verify/{code}", response_model=VerificationOut, summary="Public certificate verification")
def verify(code: str, db: Session = Depends(get_db)):
    """Unauthenticated on purpose: this is the URL printed in the certificate's QR code.
    Exposes only name/course/issuer/date, never the email."""
    cert = db.scalar(
        select(Certificate).where(Certificate.verification_code == code, Certificate.status == CertStatus.GENERATED)
    )
    if not cert:
        return VerificationOut(valid=False)
    job = db.get(Job, cert.job_id)
    return VerificationOut(valid=True, recipient_name=cert.recipient_name, course_name=job.course_name,
                           issuer_name=job.issuer_name, issue_date=job.issue_date)


router.include_router(secured)
