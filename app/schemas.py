from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, EmailStr, Field, TypeAdapter, field_validator

from .models import CertStatus, JobStatus


class RecipientIn(BaseModel):
    """Strict model used to validate ONE recipient. Not used for the request body itself,
    so a single bad row never rejects the whole batch."""

    name: str = Field(min_length=1, max_length=120)
    email: EmailStr | None = None
    @field_validator("name")
    @classmethod
    def _clean_name(cls, v: str) -> str:
        v = " ".join(v.split())  # collapse whitespace
        if not v:
            raise ValueError("name must not be blank")
        if any(ord(c) < 32 for c in v):
            raise ValueError("name contains control characters")
        try:
            v.encode("cp1252")  # the template's built-in PDF fonts are WinAnsi (Latin) only
        except UnicodeEncodeError:
            raise ValueError("name contains characters the certificate font cannot render (Latin scripts only)")
        return v


class JobCreate(BaseModel):
    course_name: str = Field(min_length=1, max_length=200, examples=["Advanced Python Bootcamp"])
    issuer_name: str = Field(min_length=1, max_length=200, examples=["Aereo Academy"])
    issue_date: date = Field(default_factory=date.today)
    # Deliberately loose: each item is validated individually by the service.
    recipients: list[dict[str, Any]] = Field(min_length=1)


class ProgressOut(BaseModel):
    total: int
    generated: int
    failed: int
    pending: int
    percent_complete: float


class JobOut(BaseModel):
    id: str
    status: JobStatus
    course_name: str
    issuer_name: str
    issue_date: date
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    progress: ProgressOut
    links: dict[str, str]


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    row_index: int
    recipient_name: str
    recipient_email: str | None
    status: CertStatus
    error: str | None
    verification_code: str
    generated_at: datetime | None
    download_url: str | None = None


class CertificatePage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CertificateOut]


class VerificationOut(BaseModel):
    valid: bool
    recipient_name: str | None = None
    course_name: str | None = None
    issuer_name: str | None = None
    issue_date: date | None = None


recipient_adapter = TypeAdapter(RecipientIn)
