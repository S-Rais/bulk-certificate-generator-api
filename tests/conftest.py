import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        storage_dir=tmp_path / "storage",
        eager=True,  # run jobs inside the request => deterministic tests
        api_key="",
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def make_payload(n=3, **over):
    body = {
        "course_name": "Advanced Python Bootcamp",
        "issuer_name": "Aereo Academy",
        "issue_date": "2026-09-30",
        "recipients": [{"name": f"Person {i}", "email": f"p{i}@example.com"} for i in range(n)],
    }
    body.update(over)
    return body
