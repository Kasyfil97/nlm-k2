"""Kebijakan lapis config (Unit 2c): setiap afordansi laptop mati di luar `ENVIRONMENT=local`.

Semua digerbangi di lapis config, bukan di tempat pemanggilan. Alasannya sama untuk semuanya: nama
berkas dan `file_url` adalah masukan yang dikuasai pemanggil, jadi kalau gerbangnya ada di tempat
pemakaian, sebuah request bisa menyetirnya hanya dengan menamai berkas atau memilih URL.
"""

import pytest
from pydantic import ValidationError

from ocr_common.clients.fetch_url import STRICT_URL_POLICY, FetchUrlError, UrlPolicy, fetch
from ocr_common.config import DEFAULT_MAX_UPLOAD_BYTES, BaseServiceSettings, PipelineSettings
from ocr_common.errors import BadRequest, PayloadTooLarge
from ocr_common.image_validation import validate_image
from ocr_common.testing import TEST_API_KEY

JPEG = b"\xff\xd8fake-jpeg-bytes"


def settings(**overrides) -> BaseServiceSettings:
    base = {"api_key": "a-real-looking-key", "environment": "local", "_env_file": None}
    # Soal penekanan di baris terakhir: sebaran **dict ke pydantic-settings membuat ty mencocokkan
    # dict itu terhadap SETIAP parameter kata-kunci privatnya (_env_file, _cli_*, _secrets_dir,
    # ~40 buah) dan mengeluh sekali per parameter -- satu baris ini sendiri menghasilkan ~50
    # diagnostik. Itu batas pemeriksa tipe terhadap sebaran, bukan cacat: setiap kunci di sini
    # harfiah dan diuji nilainya oleh uji di bawahnya.
    return BaseServiceSettings(**{**base, **overrides})  # ty: ignore[invalid-argument-type]


def deployed(**overrides) -> BaseServiceSettings:
    return settings(environment="production", **overrides)


# --- unggahan: batas dan tipe (§13.1) ------------------------------------------------------


def test_upload_limit_is_five_megabytes():
    assert DEFAULT_MAX_UPLOAD_BYTES == 5 * 1024 * 1024
    assert settings().max_upload_bytes == 5 * 1024 * 1024


def test_pdf_is_rejected_by_default():
    with pytest.raises(BadRequest, match="Unsupported content type"):
        validate_image("application/pdf", b"%PDF-1.4 ...", settings())


def test_pdf_is_admitted_only_by_the_switch_never_by_the_list():
    """R34a(a): perilaku bawaan tetap 400. R34a(c): jalurnya tetap dijalankan sekali dengan env
    menyala, supaya kode yang dipertahankan tidak jadi kode mati yang tak teruji."""
    assert "application/pdf" not in settings().effective_content_types
    assert "application/pdf" in settings(pdf_enabled=True).effective_content_types
    validate_image("application/pdf", b"%PDF-1.4 ...", settings(pdf_enabled=True))


def test_jpeg_and_png_pass_and_anything_else_does_not():
    for good in ("image/jpeg", "image/png"):
        validate_image(good, JPEG, settings())
    for bad in ("image/gif", "image/webp", "text/html", ""):
        with pytest.raises(BadRequest):
            validate_image(bad, JPEG, settings())


def test_an_oversized_upload_is_413_with_an_indonesian_message():
    with pytest.raises(PayloadTooLarge, match="Kartu Keluarga"):
        validate_image("image/jpeg", b"x" * (5 * 1024 * 1024 + 1), settings())


# --- file_url: gagal-tertutup (R18a) -------------------------------------------------------


def test_the_default_policy_denies_every_host():
    assert STRICT_URL_POLICY.host_allowed("storage.example.com") is False
    assert STRICT_URL_POLICY.host_allowed("anything") is False


def test_an_empty_allowlist_denies_rather_than_allowing_public_addresses():
    """Ini membalik pembacaan §13.1 ("kosong = hanya alamat publik") dengan sengaja: gagal-terbuka
    pada variabel yang lupa diisi adalah bawaan yang salah untuk satu-satunya jalur konten masuk."""
    assert deployed().file_url_policy.host_allowed("storage.example.com") is False
    assert settings().file_url_policy.host_allowed("storage.example.com") is True, "lokal tetap longgar"


def test_a_listed_host_passes_and_a_subdomain_entry_matches():
    policy = deployed(file_url_allowed_hosts="storage.example.com,.internal").file_url_policy
    assert policy.host_allowed("storage.example.com") is True
    assert policy.host_allowed("bucket.internal") is True
    assert policy.host_allowed("storage.example.com.evil.test") is False


def test_https_is_required_outside_local():
    assert deployed(file_url_allowed_hosts="h").file_url_policy.scheme_allowed("http") is False
    assert deployed(file_url_allowed_hosts="h").file_url_policy.scheme_allowed("https") is True
    assert settings().file_url_policy.scheme_allowed("http") is True, "MinIO di laptop tanpa TLS"


async def test_fetch_refuses_http_under_a_strict_policy():
    policy = UrlPolicy(allowed_hosts=("storage.example.com",))
    with pytest.raises(FetchUrlError, match="https is required"):
        await fetch("http://storage.example.com/a.jpg", limit=1000, policy=policy)


async def test_fetch_refuses_an_unlisted_host_before_resolving_it():
    with pytest.raises(FetchUrlError, match="not allowed"):
        await fetch("https://evil.test/a.jpg", limit=1000, policy=UrlPolicy(allowed_hosts=("good.test",)))


def test_loopback_and_link_local_are_never_reachable_however_the_host_is_listed():
    policy = deployed(file_url_allowed_hosts="storage.example.com").file_url_policy
    for never in ("127.0.0.1", "169.254.169.254", "::1", "224.0.0.1"):
        assert policy.address_allowed(never, "storage.example.com") is False


def test_a_private_address_needs_the_host_listed_literally_not_by_wildcard():
    """Object store di dalam cluster beralamat privat, dan itulah alasan allow-list ada -- menolaknya
    mentah-mentah membuat fiturnya tak berguna. Tapi entri wildcard tidak mewarisi kepercayaan itu:
    siapa pun yang menguasai DNS di bawah sufiksnya bisa mengarahkan nama ke alamat internal mana pun."""
    exact = deployed(file_url_allowed_hosts="minio.ocr.svc.cluster.local").file_url_policy
    assert exact.address_allowed("10.0.0.5", "minio.ocr.svc.cluster.local") is True

    wildcard = deployed(file_url_allowed_hosts=".svc.cluster.local").file_url_policy
    assert wildcard.host_allowed("evil.svc.cluster.local") is True, "host-nya memang cocok"
    assert wildcard.address_allowed("10.0.0.5", "evil.svc.cluster.local") is False, "alamatnya tidak"
    assert wildcard.address_allowed("93.184.216.34", "evil.svc.cluster.local") is True, "publik tetap boleh"


def test_a_downloading_service_refuses_to_start_without_an_allowlist():
    deployed(file_url_allowed_hosts="storage.example.com").require_file_url_allowlist()
    settings().require_file_url_allowlist()
    with pytest.raises(ValueError, match="FILE_URL_ALLOWED_HOSTS must list"):
        deployed().require_file_url_allowlist()


# --- kunci API placeholder (R29) -----------------------------------------------------------


@pytest.mark.parametrize("weak", ["changeme", "CHANGEME", " your-api-key ", "replace-me", "todo"])
def test_a_named_placeholder_api_key_refuses_to_start_outside_local(weak):
    with pytest.raises(ValidationError, match="placeholder"):
        deployed(api_key=weak)


@pytest.mark.parametrize("short", ["x", "k", "secret", "dev-key-123"])
def test_a_key_too_short_to_be_real_refuses_too(short):
    """Aturan panjang yang menanggung beban: apa yang salah dari `x` bukan namanya, melainkan
    bahwa ia terlalu pendek untuk pernah menjadi kunci sungguhan. Daftar nama selalu bisa
    dielakkan dengan satu salah ketik lagi."""
    with pytest.raises(ValidationError, match="shorter than 16"):
        deployed(api_key=short)


def test_a_weak_key_hidden_in_the_rotation_list_is_caught_too():
    with pytest.raises(ValidationError, match="placeholder"):
        deployed(api_key="a-real-looking-key", api_keys="another-real-one,changeme")


def test_a_placeholder_is_fine_locally():
    assert settings(api_key="changeme").api_key == "changeme"


# --- afordansi dev (R8a) -------------------------------------------------------------------


def test_testing_endpoints_refuse_to_start_outside_local():
    assert settings(testing_endpoints=True).testing_endpoints is True
    with pytest.raises(ValidationError, match="TESTING_ENDPOINTS"):
        deployed(testing_endpoints=True)


def test_simulation_hooks_only_fire_locally():
    assert settings().simulation_hooks_enabled is True
    assert deployed().simulation_hooks_enabled is False


# --- audit PII (R27) -----------------------------------------------------------------------


def pipeline(**overrides) -> PipelineSettings:
    base = {
        "api_key": "a-real-looking-key",
        "environment": "production",
        "database_url": "postgresql+asyncpg://u:p@db/x",
        "orchestration_outcome_table": "orchestration_extract_ocr",
        "_env_file": None,
    }
    # Soal penekanan di baris terakhir: sebaran **dict ke pydantic-settings membuat ty mencocokkan
    # dict itu terhadap SETIAP parameter kata-kunci privatnya (_env_file, _cli_*, _secrets_dir,
    # ~40 buah) dan mengeluh sekali per parameter -- satu baris ini sendiri menghasilkan ~50
    # diagnostik. Itu batas pemeriksa tipe terhadap sebaran, bukan cacat: setiap kunci di sini
    # harfiah dan diuji nilainya oleh uji di bawahnya.
    return PipelineSettings(**{**base, **overrides})  # ty: ignore[invalid-argument-type]


def test_pii_audit_guard_blocks_deployment_while_the_audit_is_unimplemented():
    with pytest.raises(ValueError, match="PII_AUDIT_IMPLEMENTED=false"):
        pipeline().require_pii_audit()
    pipeline(pii_audit_implemented=True).require_pii_audit()
    PipelineSettings(api_key=TEST_API_KEY, environment="local", _env_file=None).require_pii_audit()


def test_the_guard_does_not_key_on_the_encryption_key_being_present():
    """Sebuah string berbentuk Fernet di Secret produksi akan memuaskan pemeriksaan semacam itu
    tanpa satu byte pun terenkripsi -- persis keadaan yang hendak dicegah."""
    assert not hasattr(pipeline(), "pii_encryption_key") or pipeline().pii_audit_implemented is False
    with pytest.raises(ValueError, match="PII_AUDIT_IMPLEMENTED=false"):
        pipeline().require_pii_audit()
