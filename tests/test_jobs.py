import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services import pdf as pdf_module
from tests.conftest import make_payload


# ------------------------------------------------------------ creating a job
def test_create_job_returns_202_and_job_body(client):
    r = client.post("/api/v1/jobs", json=make_payload(3))
    assert r.status_code == 202
    body = r.json()
    assert body["id"] and body["progress"]["total"] == 3
    assert body["links"]["download_zip"].endswith("/download")


def test_bulk_in_single_request(client):
    r = client.post("/api/v1/jobs", json=make_payload(250))
    assert r.json()["progress"]["generated"] == 250


def test_idempotency_key_prevents_duplicate_jobs(client):
    h = {"Idempotency-Key": "abc-123"}
    first = client.post("/api/v1/jobs", json=make_payload(2), headers=h)
    second = client.post("/api/v1/jobs", json=make_payload(2), headers=h)
    assert first.status_code == 202 and second.status_code == 200
    assert first.json()["id"] == second.json()["id"]


def test_recipient_limit_enforced(settings):
    settings.max_recipients_per_job = 5
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/v1/jobs", json=make_payload(6)).status_code == 413


# ---------------------------------------------------------------- validation
@pytest.mark.parametrize("override", [
    {"recipients": []},
    {"course_name": ""},
    {"issuer_name": "   " * 0},
    {"issue_date": "not-a-date"},
    {"recipients": "nope"},
])
def test_request_level_validation_returns_422(client, override):
    assert client.post("/api/v1/jobs", json=make_payload(1, **override)).status_code == 422


def test_bad_recipients_fail_individually_good_ones_succeed(client):
    recipients = [
        {"name": "Valid One", "email": "ok@example.com"},
        {"name": "", "email": "x@example.com"},                  # blank name
        {"name": "Bad Email", "email": "not-an-email"},          # bad email
        {"email": "noname@example.com"},                         # missing name
        {"name": "Valid Two"},                                   # email is optional
        {"name": "Dup", "email": "OK@example.com"},              # duplicate of row 0 (case-insens.)
        {"name": "ராஜா"},                                        # unsupported script
    ]
    job = client.post("/api/v1/jobs", json=make_payload(recipients=recipients)).json()
    assert job["status"] == "completed_with_errors"
    assert job["progress"] == {"total": 7, "generated": 2, "failed": 5, "pending": 0, "percent_complete": 100.0}

    failed = client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"status": "failed"}).json()
    errors = {i["row_index"]: i["error"] for i in failed["items"]}
    assert set(errors) == {1, 2, 3, 5, 6}
    assert "duplicate email" in errors[5]
    assert "Latin" in errors[6]


def test_all_invalid_job_is_failed_immediately(client):
    job = client.post("/api/v1/jobs", json=make_payload(recipients=[{"name": ""}, {}])).json()
    assert job["status"] == "failed" and job["progress"]["generated"] == 0


def test_name_whitespace_is_normalised(client):
    job = client.post("/api/v1/jobs", json=make_payload(recipients=[{"name": "  Ada    Lovelace "}])).json()
    items = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"]
    assert items[0]["recipient_name"] == "Ada Lovelace"


# ---------------------------------------------------------------- generation
def test_generated_file_is_a_real_pdf_containing_metadata(client, settings):
    job = client.post("/api/v1/jobs", json=make_payload(1)).json()
    cert = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]
    assert cert["status"] == "generated"
    r = client.get(cert["download_url"])
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert r.content.startswith(b"%PDF") and b"Person 0" in r.content  # title metadata is uncompressed


def test_very_long_name_and_course_do_not_break_rendering(client):
    p = make_payload(recipients=[{"name": "W" * 120}], course_name="C" * 200)
    assert client.post("/api/v1/jobs", json=p).json()["status"] == "completed"


# ------------------------------------------------------------ status/progress
def test_status_endpoint_and_404(client):
    job = client.post("/api/v1/jobs", json=make_payload(2)).json()
    r = client.get(f"/api/v1/jobs/{job['id']}")
    assert r.status_code == 200
    assert r.json()["status"] == "completed" and r.json()["completed_at"]
    assert client.get("/api/v1/jobs/does-not-exist").status_code == 404


def test_progress_is_visible_mid_flight(settings):
    """Non-eager mode: submit, and observe the job reach completion by polling."""
    import time
    settings.eager = False
    with TestClient(create_app(settings)) as c:
        job = c.post("/api/v1/jobs", json=make_payload(30)).json()
        assert job["status"] in ("pending", "processing", "completed")
        for _ in range(100):
            cur = c.get(f"/api/v1/jobs/{job['id']}").json()
            if cur["status"] == "completed":
                break
            time.sleep(0.05)
        assert cur["progress"]["generated"] == 30 and cur["progress"]["percent_complete"] == 100.0


def test_pagination_and_status_filter(client):
    job = client.post("/api/v1/jobs", json=make_payload(7)).json()
    page = client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"limit": 3, "offset": 3}).json()
    assert page["total"] == 7 and [i["row_index"] for i in page["items"]] == [3, 4, 5]


# ------------------------------------------------- individual failure handling
@pytest.fixture
def flaky_renderer(monkeypatch):
    real = pdf_module.render_certificate
    state = {"fail": True}

    def render(out_path, **kw):
        if state["fail"] and kw["recipient_name"] == "Person 1":
            raise RuntimeError("disk exploded")
        return real(out_path, **kw)

    monkeypatch.setattr(pdf_module, "render_certificate", render)
    return state


def test_one_generation_failure_does_not_stop_the_others(client, flaky_renderer):
    job = client.post("/api/v1/jobs", json=make_payload(3)).json()
    assert job["status"] == "completed_with_errors"
    assert job["progress"]["generated"] == 2 and job["progress"]["failed"] == 1
    failed = client.get(f"/api/v1/jobs/{job['id']}/certificates", params={"status": "failed"}).json()["items"]
    assert failed[0]["recipient_name"] == "Person 1" and "disk exploded" in failed[0]["error"]
    assert failed[0]["download_url"] is None


def test_retry_recovers_transient_failures(client, flaky_renderer):
    job = client.post("/api/v1/jobs", json=make_payload(3)).json()
    flaky_renderer["fail"] = False  # the transient problem is fixed
    r = client.post(f"/api/v1/jobs/{job['id']}/retry")
    assert r.status_code == 202 and r.json()["status"] == "completed"
    assert r.json()["progress"]["generated"] == 3


def test_validation_failures_are_not_retryable(client):
    job = client.post("/api/v1/jobs", json=make_payload(recipients=[{"name": "ok"}, {"name": ""}])).json()
    assert client.post(f"/api/v1/jobs/{job['id']}/retry").status_code == 409


# ------------------------------------------------------------------ retrieval
def test_download_zip_has_pdfs_and_manifest_with_failures(client):
    recipients = [{"name": "Good Person", "email": "g@example.com"}, {"name": "", "email": "b@example.com"}]
    job = client.post("/api/v1/jobs", json=make_payload(recipients=recipients)).json()
    r = client.get(f"/api/v1/jobs/{job['id']}/download")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "manifest.csv" in names and sum(n.endswith(".pdf") for n in names) == 1
    manifest = zf.read("manifest.csv").decode()
    assert "Good Person" in manifest and "failed" in manifest


def test_download_zip_conflict_when_nothing_generated(client):
    job = client.post("/api/v1/jobs", json=make_payload(recipients=[{"name": ""}])).json()
    assert client.get(f"/api/v1/jobs/{job['id']}/download").status_code == 409


def test_single_download_errors(client):
    assert client.get("/api/v1/certificates/nope/download").status_code == 404
    job = client.post("/api/v1/jobs", json=make_payload(recipients=[{"name": ""}])).json()
    cid = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]["id"]
    assert client.get(f"/api/v1/certificates/{cid}/download").status_code == 409


# ------------------------------------------------------- verification & auth
def test_public_verification_hides_email(client):
    job = client.post("/api/v1/jobs", json=make_payload(1)).json()
    code = client.get(f"/api/v1/jobs/{job['id']}/certificates").json()["items"][0]["verification_code"]
    v = client.get(f"/api/v1/verify/{code}").json()
    assert v["valid"] and v["recipient_name"] == "Person 0" and "email" not in v
    assert client.get("/api/v1/verify/forged").json() == {
        "valid": False, "recipient_name": None, "course_name": None, "issuer_name": None, "issue_date": None}


def test_api_key_auth(settings):
    settings.api_key = "s3cret"
    with TestClient(create_app(settings)) as c:
        assert c.post("/api/v1/jobs", json=make_payload(1)).status_code == 401
        assert c.post("/api/v1/jobs", json=make_payload(1), headers={"X-API-Key": "wrong"}).status_code == 401
        assert c.post("/api/v1/jobs", json=make_payload(1), headers={"X-API-Key": "s3cret"}).status_code == 202
        assert c.get("/health").status_code == 200  # ops + verification stay public


# -------------------------------------------------------------------- recovery
def test_unfinished_jobs_are_resumed_after_restart(settings):
    """Simulate a crash: job + rows exist as PENDING, no PDFs. A fresh app must finish them."""
    import time
    from app.models import Job, JobStatus
    settings.eager = False
    app1 = create_app(settings)
    with TestClient(app1) as c:
        job_id = c.post("/api/v1/jobs", json=make_payload(5)).json()["id"]
        for _ in range(100):
            if c.get(f"/api/v1/jobs/{job_id}").json()["status"] == "completed":
                break
            time.sleep(0.05)
    # rewind the job to look like it died mid-way
    with app1.state.session_factory() as db:
        from app.models import Certificate, CertStatus
        j = db.get(Job, job_id)
        j.status = JobStatus.PROCESSING
        for cert in j.certificates[2:]:
            cert.status, cert.file_path = CertStatus.PENDING, None
        db.commit()
    with TestClient(create_app(settings)) as c2:
        for _ in range(100):
            cur = c2.get(f"/api/v1/jobs/{job_id}").json()
            if cur["status"] == "completed":
                break
            time.sleep(0.05)
        assert cur["status"] == "completed" and cur["progress"]["generated"] == 5
