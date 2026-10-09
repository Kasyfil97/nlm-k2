"""Elastic APM for every service, on when `ELASTIC_APM_SERVER_URL` is set.

The agent captures request bodies, headers and the local variables of a failing frame by default. Here
those would be card uploads and the 26 extracted fields, sent to a collector whose retention and readers
differ from the database's (docs/decisions/2026-09-26-keputusan-fase-0.md §4). So all three are off, and
so is central configuration, through which Kibana could switch body capture back on at runtime. The
agent lets `ELASTIC_APM_*` environment variables override what is passed here; a variable that turns
any of them back on makes the service refuse to start instead.
"""

from typing import Any

from fastapi import FastAPI

from ocr_common.config import BaseServiceSettings

# Probes and scrapes: one transaction every few seconds per pod, and nothing to learn from them.
IGNORED_URLS = ("/health", "/ready", "/metrics")

_PRIVATE = {"capture_body": "off", "capture_headers": False, "collect_local_variables": "off", "central_config": False}


def apm_config(settings: BaseServiceSettings, service_name: str | None) -> dict[str, Any]:
    """The agent configuration: the `ELASTIC_APM_*` settings plus the privacy defaults above. The service
    name defaults to `nilam-ocr-kk-<service>`, so each of the five shows up on its own in Kibana."""
    config: dict[str, Any] = {
        "SERVER_URL": settings.elastic_apm_server_url,
        "SERVICE_NAME": settings.elastic_apm_service_name or f"nilam-ocr-kk-{service_name or 'service'}",
        "ENVIRONMENT": settings.elastic_apm_environment or settings.environment,
        "TRANSACTION_IGNORE_URLS": list(IGNORED_URLS),
        **{key.upper(): value for key, value in _PRIVATE.items()},
    }
    if settings.elastic_apm_secret_token:
        config["SECRET_TOKEN"] = settings.elastic_apm_secret_token
    fields = [name.strip() for name in settings.elastic_apm_sanitize_field_names.split(",") if name.strip()]
    if fields:
        config["SANITIZE_FIELD_NAMES"] = fields
    return config


def install_apm(app: FastAPI, settings: BaseServiceSettings, service_name: str | None) -> None:
    """Add the APM middleware when `ELASTIC_APM_SERVER_URL` is set; otherwise do nothing."""
    if not settings.elastic_apm_server_url:
        return
    from elasticapm.contrib.starlette import ElasticAPM, make_apm_client  # lazy: only needed when APM is on

    client = make_apm_client(apm_config(settings, service_name))
    overridden = sorted(key for key, value in _PRIVATE.items() if getattr(client.config, key) != value)
    if overridden:
        client.close()
        raise ValueError(
            f"ELASTIC_APM_{', ELASTIC_APM_'.join(key.upper() for key in overridden)} must stay at its default "
            "here: it would send uploads or extracted fields to the APM collector"
        )
    app.add_middleware(ElasticAPM, client=client)
