"""One async SQLAlchemy engine per database URL, shared by everything in the process."""

import os
from typing import Any

from sqlalchemy import JSON, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

JSON_TYPE = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")

# The PostgreSQL schema every table of this repository lives in (0003 moved them out of `public`), and the prefix
# of every table name (0003): the client's naming, `nilam_ocr_kk.nilam_ocr_jobs`, as in nilam.
PIPELINE_SCHEMA = "nilam_ocr_kk"
TABLE_PREFIX = "nilam_"

# Sentinel URL untuk koneksi Cloud SQL Python Connector — bukan URL sungguhan, hanya penanda
# bahwa CLOUDSQL_INSTANCE / DB_NAME / DB_USER / DB_PASS / CLOUDSQL_IP_TYPE yang dipakai.
CLOUDSQL_SENTINEL = "cloudsql"


_engines: dict[str, AsyncEngine] = {}
_factories: dict[str, async_sessionmaker] = {}


def _connect_args(url: str) -> dict:
    """TLS stays on asyncpg's default; `DATABASE_SSL_DISABLE=true` turns it off for a local Postgres only."""
    if url == CLOUDSQL_SENTINEL:
        return {}
    if url.startswith("postgresql+asyncpg") and os.environ.get("DATABASE_SSL_DISABLE", "").lower() == "true":
        return {"ssl": False}
    return {}


def _create_cloudsql_engine() -> AsyncEngine:
    """Engine via Cloud SQL Python Connector. Dibaca dari env CLOUDSQL_INSTANCE, DB_NAME, dll.

    DB_USER dan DB_PASS kosong = IAM database authentication (service account identity otomatis).
    DB_PASS diisi = password authentication biasa.
    """
    try:
        from google.cloud.sql.connector import IPTypes, create_async_connector
    except ImportError as exc:
        raise RuntimeError(
            "google-cloud-sql-connector tidak terinstall. "
            "Tambahkan cloud-sql-python-connector[asyncpg] ke requirements."
        ) from exc

    instance = os.environ.get("CLOUDSQL_INSTANCE", "")
    db_name = os.environ.get("DB_NAME", "")
    db_user: str | None = os.environ.get("DB_USER") or None
    db_pass: str | None = os.environ.get("DB_PASS") or None
    ip_type_str = os.environ.get("CLOUDSQL_IP_TYPE", "PRIVATE").upper()

    if not instance or not db_name:
        raise RuntimeError("CLOUDSQL_INSTANCE dan DB_NAME harus di-set ketika memakai Cloud SQL connector")

    ip_type = IPTypes.PRIVATE if ip_type_str == "PRIVATE" else IPTypes.PUBLIC
    use_iam_auth = db_pass is None

    # Connector dibuat lazy di dalam _creator, bukan di sini: Connector() default memulai event loop
    # sendiri di thread background (agar .connect() sync bekerja), sehingga connect_async() dari app
    # loop melempar ConnectorLoopError. create_async_connector() mengikat Connector ke event loop yang
    # sedang berjalan — yaitu app loop tempat SQLAlchemy memanggil creator ini.
    connector: Any = None

    async def _creator() -> Any:
        nonlocal connector
        if connector is None:
            connector = await create_async_connector()
        return await connector.connect_async(
            instance,
            "asyncpg",
            user=db_user,
            password=db_pass,
            db=db_name,
            enable_iam_auth=use_iam_auth,
            ip_type=ip_type,
        )

    return create_async_engine(
        "postgresql+asyncpg://",
        async_creator=_creator,
        pool_pre_ping=True,
        hide_parameters=True,
    )


def get_engine(url: str) -> AsyncEngine:
    """The engine for `url`, created on first use with pool pre-ping. SQLite (the tests) has no schemas, so
    there the tables of `PIPELINE_SCHEMA` are used without one. `url == CLOUDSQL_SENTINEL` pakai
    Cloud SQL Python Connector dengan env vars CLOUDSQL_INSTANCE / DB_NAME / DB_USER / DB_PASS."""
    if url not in _engines:
        if url == CLOUDSQL_SENTINEL:
            _engines[url] = _create_cloudsql_engine()
        else:
            options = {"schema_translate_map": {PIPELINE_SCHEMA: None}} if url.startswith("sqlite") else {}
            _engines[url] = create_async_engine(
                url,
                pool_pre_ping=True,
                hide_parameters=True,
                connect_args=_connect_args(url),
                execution_options=options,
            )
    return _engines[url]


def get_session_factory(url: str) -> async_sessionmaker:
    """An `async_sessionmaker` bound to the engine for `url`."""
    if url not in _factories:
        _factories[url] = async_sessionmaker(get_engine(url), expire_on_commit=False)
    return _factories[url]


async def check_connection(url: str) -> None:
    """Runs `SELECT 1`; raises when the database is unreachable (readiness, startup)."""
    async with get_engine(url).connect() as conn:
        await conn.execute(text("SELECT 1"))


async def dispose_engines() -> None:
    """Closes every engine's pool; call at shutdown and between tests."""
    for engine in _engines.values():
        await engine.dispose()
    _engines.clear()
    _factories.clear()
