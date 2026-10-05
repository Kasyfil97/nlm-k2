"""Settings of every service, read from the environment (and `.env` locally) with pydantic-settings.

`BaseServiceSettings` is what all five services share; `PipelineSettings` adds what the three
asynchronous stages need. Guards on `ENVIRONMENT` make a deployed service refuse to start with a
laptop-only configuration (mock backends, auth disabled, localhost addresses).
"""

from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from ocr_common.clients.fetch_url import UrlPolicy

Environment = Literal["local", "dev", "staging", "production"]
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
DEFAULT_JOB_LEASE_SECONDS = 300.0
DEFAULT_MAX_UPLOAD_BYTES = 5 * 1024 * 1024
# Two rules, because "placeholder" has two shapes. A name blocklist catches the values that travel
# in `.env.example` and in copied deployment manifests; a minimum length catches everything else,
# including the one-character keys that are obviously stand-ins but belong to no list. The length
# rule is the load-bearing one -- a blocklist can always be sidestepped by one more typo.
MIN_API_KEY_LENGTH = 16
PLACEHOLDER_API_KEYS = frozenset(
    {"changeme", "change-me", "changemechangeme", "your-api-key", "your_api_key", "replace-me", "todo", "example"}
)


class BaseServiceSettings(BaseSettings):
    """Settings shared by all services: API keys, environment, upload limits, `file_url` policy, logging."""

    model_config = SettingsConfigDict(env_file=(".env", ".env.local"), extra="ignore", populate_by_name=True)

    api_key: str = Field(..., min_length=1)
    api_keys: str = ""
    auth_disabled: bool = False

    log_format: Literal["json", "text"] | None = None
    log_level: str = "INFO"

    elasticsearch_enabled: bool = False
    elasticsearch_url: str | None = None
    elasticsearch_index: str = "ocr-logs"
    elasticsearch_api_key: str | None = None
    elasticsearch_username: str | None = None
    elasticsearch_password: str | None = None

    environment: Environment = "production"
    service_base_url: str | None = None
    port: int = 8000

    # 5 MB (§13.1): a Kartu Keluarga photo is larger than a tax card. Anything above is refused
    # with 413 before any model runs.
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    # §13.1, as in nilam: JPEG, PNG or PDF. Of a PDF only the first page is read -- guardrails
    # judges it and extraction reads it -- because a Kartu Keluarga is one sheet and the layout
    # parser reads one page frame; the orchestrator refuses more than `MAX_DOCUMENT_PAGES`.
    allowed_content_types: list[str] = ["image/jpeg", "image/jpg", "image/png", "application/pdf"]
    file_url_allowed_hosts: str = ""
    file_url_allow_http: bool = False
    field_confidence_threshold: float = Field(0.5, ge=0, le=1)
    # The `-test` endpoints (orchestrator `/v1/extract-ocr-test`, `/v1/<stage>/jobs-test`): the same pipeline on
    # the `testing_*` tables, without callbacks or writes to the orchestrator's tables. For the ML team's
    # load tests on dev; off everywhere else, and then the routes do not exist.
    testing_endpoints: bool = False

    @property
    def is_local(self) -> bool:
        """True for `ENVIRONMENT=local`: the laptop mode where the safety guards are off."""
        return self.environment == "local"

    @property
    def accepted_api_keys(self) -> tuple[str, ...]:
        """Keys a caller may present: `API_KEY` (also the key this service sends to the others) plus the
        comma-separated `API_KEYS`. Rotation: add the new key to `API_KEYS` everywhere, move the callers,
        make it `API_KEY`, drop the old one."""
        extra = tuple(key.strip() for key in self.api_keys.split(",") if key.strip())
        return (self.api_key, *(key for key in extra if key != self.api_key))

    @property
    def effective_log_format(self) -> Literal["json", "text"]:
        """`LOG_FORMAT` when set; otherwise text on a laptop and JSON (for Cloud Logging) when deployed."""
        return self.log_format or ("text" if self.is_local else "json")

    @property
    def file_url_policy(self) -> UrlPolicy:
        """The `UrlPolicy` for `file_url` downloads, from `FILE_URL_ALLOWED_HOSTS` and the environment.

        Outside local this fails closed three ways at once: an empty allow-list denies every URL,
        plain `http` is refused, and a resolved private or loopback address is refused even when the
        hostname is listed.
        """
        hosts = tuple(
            host.strip().lower().rstrip(".") for host in self.file_url_allowed_hosts.split(",") if host.strip()
        )
        return UrlPolicy(
            allowed_hosts=hosts,
            allow_private=self.is_local,
            allow_http=self.is_local or self.file_url_allow_http,
            allow_any_host=self.is_local,
        )

    def require_file_url_allowlist(self) -> None:
        """Raises outside local when `FILE_URL_ALLOWED_HOSTS` is empty. Called by the services that
        download: orchestrator and extraction always, guardrails when `GUARDRAILS_FETCH_URL`."""
        if self.is_local or self.file_url_allowed_hosts.strip():
            return
        raise ValueError(
            "FILE_URL_ALLOWED_HOSTS must list the hosts this service may download from when "
            f"ENVIRONMENT={self.environment}: an empty list denies every file_url, so starting without "
            "one would leave the download path dead (set ENVIRONMENT=local for local development)"
        )

    def require_outside_local(self, **values: object) -> None:
        """Raises unless every given value is set, when not local; used by the subclasses' validators."""
        if self.is_local:
            return
        missing = [name.upper() for name, value in values.items() if not value]
        if missing:
            raise ValueError(
                f"{', '.join(missing)} must be set when ENVIRONMENT={self.environment} "
                "(set ENVIRONMENT=local for local development)"
            )

    def reject_localhost_outside_local(self, **urls: str | None) -> None:
        """Raises when a service URL points to localhost outside local: inside a pod that is the service itself."""
        if self.is_local:
            return
        local = [name.upper() for name, url in urls.items() if url and urlsplit(url).hostname in _LOCAL_HOSTS]
        if local:
            raise ValueError(
                f"{', '.join(local)} points to localhost, which inside a pod is this service itself; "
                f"set the real address when ENVIRONMENT={self.environment}"
            )

    def reject_mock_backend_outside_local(self, **backends: str) -> None:
        """Raises when a backend is `mock` outside local: a mock fabricates results."""
        if self.is_local:
            return
        mocked = [name.upper() for name, backend in backends.items() if backend == "mock"]
        if mocked:
            raise ValueError(
                f"{', '.join(mocked)}=mock fabricates results and is only allowed with ENVIRONMENT=local; "
                f"set a real backend when ENVIRONMENT={self.environment}"
            )

    @property
    def simulation_hooks_enabled(self) -> bool:
        """Whether the filename hooks of `ocr_common.simulation` may fire. Local only, always: the
        filename crosses the trust boundary in the multipart request, so a caller could otherwise
        steer the pipeline by naming a file."""
        return self.is_local

    @model_validator(mode="after")
    def _guard_auth(self) -> Self:
        if self.auth_disabled and not self.is_local:
            raise ValueError(
                f"AUTH_DISABLED=true is only allowed with ENVIRONMENT=local (got ENVIRONMENT={self.environment}): "
                "it turns off the X-API-Key check on every endpoint"
            )
        if not self.is_local:
            weak = sorted(
                {
                    key
                    for key in self.accepted_api_keys
                    if key.strip().lower() in PLACEHOLDER_API_KEYS or len(key.strip()) < MIN_API_KEY_LENGTH
                }
            )
            if weak:
                shown = ", ".join(repr(key) for key in weak)
                raise ValueError(
                    f"API_KEY / API_KEYS holds a placeholder or a key shorter than {MIN_API_KEY_LENGTH} "
                    f"characters ({shown}), which is how a shared example secret reaches a deployed "
                    f"environment. Set a real key when ENVIRONMENT={self.environment}"
                )
        return self

    @model_validator(mode="after")
    def _guard_elasticsearch(self) -> Self:
        if self.elasticsearch_enabled and not self.elasticsearch_url:
            raise ValueError(
                "ELASTICSEARCH_URL must be set when ELASTICSEARCH_ENABLED=true"
            )
        return self

    @model_validator(mode="after")
    def _guard_dev_affordances(self) -> Self:
        """Laptop-only affordances are gated here, at the config layer, rather than where they are
        used: a filename is caller-controlled input, so a hook that reads one must not be reachable
        by a request that merely names a file a certain way."""
        if self.is_local or not self.testing_endpoints:
            return self
        raise ValueError(
            "TESTING_ENDPOINTS=true is only allowed with ENVIRONMENT=local (got "
            f"ENVIRONMENT={self.environment}): it exposes a parallel pipeline on the testing_* tables "
            "that skips the outcome table"
        )


class PipelineSettings(BaseServiceSettings):
    """Settings of the three stage services: database, orchestrator callback or outcome table, job
    lease, outbox, stale-job reaper, hand-off by reference.
    """

    database_url: str | None = None

    orchestration_url: str | None = None
    orchestration_callback_path: str = "/v1/callbacks/stage"
    orchestration_api_key: str | None = None
    orchestration_timeout_seconds: float = 10.0
    # "stage": a callback per stage (OCR, STRUCTURING, SCORING) with `X-API-Key`.
    # "result": the orchestrator's single result callback when the request ends (completed by scoring,
    # failed at any stage), authenticated with `X-Callback-Key: ORCHESTRATION_CALLBACK_KEY`.
    orchestration_callback_format: Literal["stage", "result"] = "stage"
    orchestration_callback_key: str | None = None
    # The switch: false = no callback is sent or queued even with ORCHESTRATION_URL set, e.g. while the
    # central orchestrator has NPWP in poll mode (it then answers every callback 409 CALLBACK_NOT_EXPECTED
    # and reads GET /v1/extract-ocr/{request_id} instead).
    orchestration_callback_enabled: bool = True
    # How long the outbox keeps retrying a callback (5xx / unreachable). The central orchestrator gives up on a
    # request a fixed time after its 202 (OCR_CALLBACK_DEADLINE_SECONDS on its side) and refuses later ones.
    orchestration_callback_max_age_seconds: float = Field(600.0, gt=0)

    pipeline_retry_attempts: int = 3
    pipeline_retry_delay_seconds: float = 0.5
    pipeline_drain_timeout_seconds: float = 30.0
    pipeline_job_lease_seconds: float = Field(DEFAULT_JOB_LEASE_SECONDS, gt=0)
    orchestration_outcome_table: str = ""
    orchestration_api_events_table: str = ""
    pipeline_outbox: bool = False
    pipeline_outbox_interval_seconds: float = Field(1.0, gt=0)
    pipeline_outbox_batch: int = Field(20, gt=0)
    pipeline_outbox_lease_seconds: float = Field(30.0, gt=0)
    pipeline_outbox_max_backoff_seconds: float = Field(300.0, gt=0)
    pipeline_outbox_max_age_seconds: float = Field(24 * 3600.0, gt=0)
    pipeline_outbox_stale_after_seconds: float = Field(300.0, gt=0)
    pipeline_handoff_by_reference: bool = False
    # §8.5: flipped only by the batch that actually writes the encrypted, blind-indexed audit.
    # `require_pii_audit()` below explains why this exists rather than a check on PII_ENCRYPTION_KEY.
    pii_audit_implemented: bool = False
    # Detak: sementara job berjalan ia memperbarui `updated_at`, supaya job yang sah-berjalan-lama
    # tidak terlihat basi bagi reaper. `0` = mati, dan itulah bawaannya untuk batch ini: mekanismenya
    # dipasang sekarang karena `repository.py`, `stage.py` dan `factory.py` beku setelah gerbang R6,
    # tetapi intervalnya tidak dikarang -- ia disetel oleh batch yang menjalankan model sungguhan,
    # ketika durasi job pertama kali bisa diukur.
    pipeline_heartbeat_seconds: float = Field(0.0, ge=0)
    # Batas atas umur job, TERLEPAS dari `updated_at`. Detak menghapus satu-satunya batas atas yang
    # dulu ada (lease), jadi tanpa ini job yang macet tetapi prosesnya hidup akan berdetak selamanya,
    # tidak pernah dipanen, dan tidak pernah menulis keadaan akhir. `0` = mati.
    pipeline_job_max_runtime_seconds: float = Field(0.0, ge=0)
    pipeline_stale_jobs: bool = True
    pipeline_stale_job_interval_seconds: float = Field(30.0, gt=0)
    pipeline_stale_job_batch: int = Field(10, gt=0)

    @property
    def callbacks_enabled(self) -> bool:
        """Callbacks are only sent when the orchestrator exposes an endpoint for them and the switch
        (ORCHESTRATION_CALLBACK_ENABLED) is on. The other ways the outcome reaches the orchestrator are its
        own tables (ORCHESTRATION_OUTCOME_TABLE, ORCHESTRATION_API_EVENTS_TABLE) and GET
        /v1/extract-ocr/{request_id}."""
        return self.orchestration_callback_enabled and bool(self.orchestration_url)

    @model_validator(mode="after")
    def _guard_pipeline(self) -> Self:
        self.require_outside_local(database_url=self.database_url)
        reports_outcome = (
            self.callbacks_enabled or self.orchestration_outcome_table or self.orchestration_api_events_table
        )
        # With the switch off on purpose the orchestrator polls GET /v1/extract-ocr/{request_id}.
        if not self.is_local and self.orchestration_callback_enabled and not reports_outcome:
            raise ValueError(
                "ORCHESTRATION_URL, ORCHESTRATION_OUTCOME_TABLE or ORCHESTRATION_API_EVENTS_TABLE must be set when "
                f"ENVIRONMENT={self.environment}: without one, the orchestrator never learns how a request ended "
                "(set ORCHESTRATION_CALLBACK_ENABLED=false when it polls instead, or ENVIRONMENT=local for local "
                "development)"
            )
        self.reject_localhost_outside_local(orchestration_url=self.orchestration_url)
        if (
            self.orchestration_callback_format == "result"
            and self.callbacks_enabled
            and not self.is_local
            and not self.orchestration_callback_key
        ):
            raise ValueError(
                "ORCHESTRATION_CALLBACK_KEY must be set with ORCHESTRATION_CALLBACK_FORMAT=result: the orchestrator's "
                "result callback is authenticated with X-Callback-Key"
            )
        if self.pipeline_heartbeat_seconds and not self.pipeline_job_max_runtime_seconds:
            raise ValueError(
                "PIPELINE_HEARTBEAT_SECONDS needs PIPELINE_JOB_MAX_RUNTIME_SECONDS: a beating job is never "
                "reclaimed by the reaper, so without a ceiling a wedged job would beat forever and never "
                "reach a terminal state"
            )
        if self.pipeline_heartbeat_seconds >= self.pipeline_job_lease_seconds:
            if self.pipeline_heartbeat_seconds:
                raise ValueError(
                    "PIPELINE_HEARTBEAT_SECONDS must be well below PIPELINE_JOB_LEASE_SECONDS, otherwise the "
                    "lease expires between beats and the reaper reclaims a job that is still running"
                )
        if self.pipeline_handoff_by_reference and not self.database_url:
            raise ValueError(
                "PIPELINE_HANDOFF_BY_REFERENCE=true needs DATABASE_URL: the next stage reads this stage's "
                "result from the shared database instead of the hand-off body"
            )
        return self

    # Hidup di sini, bukan di BaseServiceSettings: ia membaca `pii_audit_implemented`, dan sebuah
    # guard di kelas yang tidak punya fieldnya akan melempar AttributeError alih-alih ValueError
    # yang dijanjikannya, pada service pertama di luar scoring yang memanggilnya.
    def require_pii_audit(self) -> None:
        """Raises outside local while the §8.5 audit is not implemented.

        The check is on `PII_AUDIT_IMPLEMENTED`, not on whether `PII_ENCRYPTION_KEY` is set: any
        Fernet-shaped string would satisfy the latter while nothing is encrypted and no audit row is
        written, which is exactly the state the guard exists to block. Only the batch that actually
        writes the encrypted, blind-indexed audit may flip this flag.
        """
        if self.is_local or self.pii_audit_implemented:
            return
        raise ValueError(
            "PII_AUDIT_IMPLEMENTED=false: the encrypted audit with a blind index over nomor_kk is not "
            f"implemented yet, so this service refuses to start with ENVIRONMENT={self.environment}. "
            "It handles NIK and names; set ENVIRONMENT=local for development"
        )
