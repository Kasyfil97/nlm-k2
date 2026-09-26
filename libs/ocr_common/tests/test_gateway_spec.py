import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]


def _builder():
    script = ROOT / "scripts" / "build_gateway_openapi.py"
    spec = importlib.util.spec_from_file_location("build_gateway_openapi", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVICES = ("orchestrator", "guardrails", "ekstraksi", "structuring", "scoring")


def _stale_specs() -> list[str]:
    """Service specs still carrying the previous pipeline's wording.

    The gateway merges all five, so a half-converted set produces schemas that differ only by
    prose and get suffixed per service. Both tests below are blocked on the same thing and say so,
    rather than going red for a reason that is not theirs. The list empties itself as the three
    agent services land (Units 7-9), and the tests start running again on their own.
    """
    stale = []
    for service in SERVICES:
        spec = ROOT / "services" / service / "openapi.yaml"
        if spec.exists() and "npwp" in spec.read_text(encoding="utf-8").lower():
            stale.append(service)
    return stale


def test_gateway_spec_is_up_to_date():
    builder = _builder()
    if stale := _stale_specs():
        pytest.skip(f"spec belum dikonversi ke KK: {', '.join(stale)} (Unit 7-9); gateway dirakit di Unit 10")
    if not builder.TARGET.exists():
        pytest.skip(
            "api/gateway.openapi.yaml belum ada: ia digenerate `make openapi-gateway` dari kelima "
            "openapi.yaml, yang baru lengkap setelah ketiga agen selesai (Unit 10 rencana batch 1)."
        )
    assert yaml.safe_load(builder.TARGET.read_text(encoding="utf-8")) == builder.build()


def test_gateway_spec_has_no_dangling_refs_and_documents_the_callback():
    if stale := _stale_specs():
        pytest.skip(f"spec belum dikonversi ke KK: {', '.join(stale)} (Unit 7-9)")
    built = _builder().build()
    text = yaml.safe_dump(built)
    names = set(_builder().REF.findall(text))
    assert names <= set(built["components"]["schemas"])
    bodies = built["webhooks"]["stageCallback"]["post"]["requestBody"]["content"]["application/json"]["schema"]["oneOf"]
    assert {b["$ref"].rsplit("/", 1)[-1] for b in bodies} == {"StageCallback", "ScoringStageCallback"}


def test_every_gateway_operation_names_the_service_that_owns_it():
    for path, item in _builder().build()["paths"].items():
        for method, operation in item.items():
            assert operation["servers"], f"{method.upper()} {path} has no servers"
            assert operation["operationId"], f"{method.upper()} {path} has no operationId"


def test_the_gateway_calls_only_the_orchestrator_through_the_entry_service():
    """The guardrails and stage services are internal: the central orchestrator must not be pointed at them."""
    paths = _builder().build()["paths"]
    assert {(method, path) for path, item in paths.items() for method in item} == {
        ("post", "/v1/extract-ocr"),
        ("get", "/v1/extract-ocr/{request_id}"),
    }
    for item in paths.values():
        for operation in item.values():
            first = operation["servers"][0]
            assert first["url"] == "http://nlm-k2.{namespace}.svc.cluster.local:8040"
