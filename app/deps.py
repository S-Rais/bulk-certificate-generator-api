import secrets
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from .config import Settings, get_settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.session_factory() as db:
        yield db


def get_runner(request: Request):
    return request.app.state.runner


def app_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_api_key(
    x_api_key: str | None = Header(default=None), settings: Settings = Depends(app_settings)
) -> None:
    if not settings.api_key:
        return  # auth disabled
    if not x_api_key or not secrets.compare_digest(x_api_key, settings.api_key):
        raise HTTPException(status_code=401, detail="invalid or missing X-API-Key")
