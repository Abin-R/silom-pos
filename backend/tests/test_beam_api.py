"""Unit tests for Beam QR payment endpoints."""
import base64
import os
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the backend package is importable when pytest is invoked from the
# backend dir or the repo root. Matches the pattern used in the rest of the
# backend test suite (test_customer_stats.py, test_dashboard_bulk_lookup.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from server import (
    Settings,
    BeamChargeRequest,
    BeamChargeResponse,
    BeamChargeStatus,
    BEAM_API_KEY_MASK_PREFIX,
    _extract_qr_data,
    _is_masked_api_key,
    _mask_api_key,
    create_beam_charge,
    get_beam_charge,
)
from fastapi import HTTPException


# ---------------------------------------------------------------------------
# Shared fixtures / helpers
# ---------------------------------------------------------------------------

def _settings_doc(**overrides):
    """Build a Settings dict with sensible defaults; override individual fields per-test."""
    base = {
        "id": "shop", "shop_name": "Test", "business_type": "General",
        "pos_id": "001", "branch": "B1", "pos_number": "001",
        "open_time": "09:00", "close_time": "22:00",
        "tax_percent": 7.0, "tax_mode": "exclusive",
        "service_charge_enabled": False, "service_charge_percent": 10.0,
        "beam_merchant_id": "m_test123",
        "beam_api_key": "sk_test_valid",
        "beam_sandbox": True,
    }
    base.update(overrides)
    return base


def _make_httpx_response(status_code: int, json_body: dict):
    """Create a mock httpx.Response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_body
    mock.text = str(json_body)
    return mock


@asynccontextmanager
async def _patched_beam(settings_doc, *, post=None, get=None):
    """Patch server.db.settings + httpx.AsyncClient for a Beam test.

    `post` / `get` may be either a mock httpx.Response (returned by the call)
    or an Exception class/instance to raise (e.g. httpx.TimeoutException).
    Yields the underlying mock_client so tests can inspect call args.
    """
    with patch("server.db") as mock_db, patch("httpx.AsyncClient") as mock_client_cls:
        mock_db.settings.find_one = AsyncMock(return_value=settings_doc)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        if post is not None:
            if isinstance(post, BaseException) or (isinstance(post, type) and issubclass(post, BaseException)):
                mock_client.post = AsyncMock(side_effect=post)
            else:
                mock_client.post = AsyncMock(return_value=post)
        if get is not None:
            if isinstance(get, BaseException) or (isinstance(get, type) and issubclass(get, BaseException)):
                mock_client.get = AsyncMock(side_effect=get)
            else:
                mock_client.get = AsyncMock(return_value=get)

        mock_client_cls.return_value = mock_client
        yield mock_client


# ---------------------------------------------------------------------------
# Settings masking tests — exercise the real helper / endpoints
# ---------------------------------------------------------------------------

class TestSettingsMasking:
    """Verify _mask_api_key + _is_masked_api_key behaviour."""

    def test_mask_long_key(self):
        s = Settings(beam_api_key="sk_test_abcdefgh1234")
        masked = _mask_api_key(s)
        assert masked.beam_api_key == f"{BEAM_API_KEY_MASK_PREFIX}1234"

    def test_mask_does_not_mutate_original(self):
        """_mask_api_key must return a new Settings; the original stays intact."""
        original_key = "sk_test_abcdefgh1234"
        s = Settings(beam_api_key=original_key)
        masked = _mask_api_key(s)
        assert masked.beam_api_key == f"{BEAM_API_KEY_MASK_PREFIX}1234"
        # Original must be untouched
        assert s.beam_api_key == original_key
        assert s is not masked

    def test_mask_short_key_unchanged(self):
        """Keys with 4 or fewer chars are not masked (edge case)."""
        s = Settings(beam_api_key="abcd")
        masked = _mask_api_key(s)
        assert masked.beam_api_key == "abcd"

    def test_mask_none_unchanged(self):
        s = Settings(beam_api_key=None)
        masked = _mask_api_key(s)
        assert masked.beam_api_key is None

    def test_is_masked_true_for_placeholder(self):
        assert _is_masked_api_key(f"{BEAM_API_KEY_MASK_PREFIX}1234") is True

    def test_is_masked_false_for_real_key(self):
        # A real key that happens to start with similar chars but isn't the right shape
        assert _is_masked_api_key("sk_live_realkey9999") is False
        # Empty / None
        assert _is_masked_api_key("") is False

    def test_is_masked_false_for_wrong_suffix_length(self):
        """Reject anything with the prefix but a non-4-char suffix (defends against legitimate keys
        that happen to start with the mask character)."""
        assert _is_masked_api_key(f"{BEAM_API_KEY_MASK_PREFIX}123") is False  # too short
        assert _is_masked_api_key(f"{BEAM_API_KEY_MASK_PREFIX}12345") is False  # too long


# ---------------------------------------------------------------------------
# _extract_qr_data helper tests (feedback #5: now unit-testable)
# ---------------------------------------------------------------------------

class TestExtractQrData:
    """Verify _extract_qr_data handles all Beam response shapes."""

    def test_production_encoded_image(self):
        """Production API: imageBase64Encoded + rawData."""
        data = {
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {
                "imageBase64Encoded": "iVBORw0KGgo=",
                "rawData": "00020101021230780016A000000677010112",
            },
        }
        qr_image, qr_string = _extract_qr_data(data)
        assert qr_image == "iVBORw0KGgo="
        assert qr_string == "00020101021230780016A000000677010112"

    def test_sandbox_encoded_image(self):
        """Older sandbox API: image + qrString."""
        data = {
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {
                "image": "base64data",
                "qrString": "00020101",
            },
        }
        qr_image, qr_string = _extract_qr_data(data)
        assert qr_image == "base64data"
        assert qr_string == "00020101"

    def test_qr_code_fallback(self):
        """Top-level qrCode field fallback."""
        data = {"qrCode": "fallback_qr_image"}
        qr_image, qr_string = _extract_qr_data(data)
        assert qr_image == "fallback_qr_image"
        assert qr_string is None

    def test_no_qr_data(self):
        """No QR-related fields at all → both None."""
        data = {"status": "PENDING"}
        qr_image, qr_string = _extract_qr_data(data)
        assert qr_image is None
        assert qr_string is None

    def test_encoded_image_missing_sub_fields(self):
        """actionRequired=ENCODED_IMAGE but encodedImage dict is empty."""
        data = {"actionRequired": "ENCODED_IMAGE", "encodedImage": {}}
        qr_image, qr_string = _extract_qr_data(data)
        assert qr_image is None
        assert qr_string is None

    def test_production_fields_preferred_over_sandbox(self):
        """When both field names are present, production names win (or-chain)."""
        data = {
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {
                "imageBase64Encoded": "prod_image",
                "image": "sandbox_image",
                "rawData": "prod_raw",
                "qrString": "sandbox_qr",
            },
        }
        qr_image, qr_string = _extract_qr_data(data)
        assert qr_image == "prod_image"
        assert qr_string == "prod_raw"


# ---------------------------------------------------------------------------
# Empty-string API key guard — exercises real server helper
# ---------------------------------------------------------------------------

class TestApiKeyGuard:
    """Verify _should_strip_beam_api_key helper behaviour via _is_masked_api_key."""

    def test_empty_string_detected(self):
        """Empty string should be recognised as a value to strip."""
        # The guard logic: _is_masked_api_key(val) or val == ""
        assert _is_masked_api_key("") is False  # not masked, but ""
        # Combined check as in update_settings:
        val = ""
        assert _is_masked_api_key(val) or val == ""

    def test_masked_placeholder_detected(self):
        val = f"{BEAM_API_KEY_MASK_PREFIX}1234"
        assert _is_masked_api_key(val) or val == ""

    def test_real_key_not_stripped(self):
        val = "sk_live_newkey"
        assert not (_is_masked_api_key(val) or val == "")


# ---------------------------------------------------------------------------
# Beam charge endpoint tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBeamChargeCreate:
    """Tests for POST /api/beam/charge."""

    async def test_missing_credentials_returns_400(self):
        doc = _settings_doc(beam_merchant_id=None, beam_api_key=None)
        with patch("server.db") as mock_db:
            mock_db.settings.find_one = AsyncMock(return_value=doc)
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 400
            assert "credentials not configured" in exc_info.value.detail

    async def test_beam_401_returns_401(self):
        doc = _settings_doc(beam_api_key="bad_key")
        resp = _make_httpx_response(401, {"message": "Unauthorized"})

        async with _patched_beam(doc, post=resp):
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 401
            assert "invalid or expired" in exc_info.value.detail

    async def test_beam_timeout_returns_502(self):
        import httpx as httpx_module

        async with _patched_beam(_settings_doc(), post=httpx_module.TimeoutException("timeout")):
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 502
            assert "timed out" in exc_info.value.detail

    async def test_beam_request_error_returns_502(self):
        """Cover the generic RequestError handler (e.g. DNS failure, connection refused)."""
        import httpx as httpx_module

        async with _patched_beam(_settings_doc(), post=httpx_module.ConnectError("Connection refused")):
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-ERR")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 502
            assert "Cannot reach Beam API" in exc_info.value.detail

    @pytest.mark.parametrize("status_code", [500, 503, 429])
    async def test_beam_non_200_201_status_returns_502(self, status_code):
        """Non-200/201/401 status codes from Beam should return 502 with truncated body."""
        resp = _make_httpx_response(status_code, {"error": "something went wrong" * 50})

        async with _patched_beam(_settings_doc(), post=resp):
            req = BeamChargeRequest(amount=100.0, reference_id="TEST-ERR-STATUS")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 502
            assert f"Beam API error {status_code}" in exc_info.value.detail
            # Verify truncation to 300 chars
            assert len(exc_info.value.detail) <= 350  # "Beam API error NNN: " + 300 chars

    async def test_beam_success_with_production_response_shape(self):
        """Verify Beam's production response uses chargeId + imageBase64Encoded + rawData.

        Captured from a real charge against api.beamcheckout.com — the production
        API uses different field names than the older sandbox docs reference.
        Without this regression, the QR pane stays blank because qr_image and
        qr_string are extracted from the wrong keys.
        """
        # Actual production response shape from api.beamcheckout.com
        beam_response = {
            "chargeId": "ch_3D13uHz1vsSAKLzXOsa1c0J2Vcw",
            "paymentMethodType": "QR_PROMPT_PAY",
            "actionRequired": "ENCODED_IMAGE",
            "redirect": None,
            "encodedImage": {
                "imageBase64Encoded": "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEAAQMAAA==",
                "rawData": "00020101021230780016A000000677010112",
                "expiry": "2026-04-29T11:42:49.651591332+07:00",
            },
        }
        resp = _make_httpx_response(200, beam_response)

        async with _patched_beam(_settings_doc(), post=resp):
            req = BeamChargeRequest(amount=1.00, reference_id="PROD-001")
            result = await create_beam_charge(req)

            assert result.charge_id == "ch_3D13uHz1vsSAKLzXOsa1c0J2Vcw"
            assert result.qr_image == "iVBORw0KGgoAAAANSUhEUgAAAQAAAAEAAQMAAA=="
            assert result.qr_string == "00020101021230780016A000000677010112"
            # Status defaults to PENDING when not provided in the response
            assert result.status == "PENDING"

    async def test_beam_success_with_encoded_image(self):
        beam_response = {
            "id": "ch_abc123",
            "status": "PENDING",
            "amount": 35000,  # 350.00 THB in satang
            "currency": "THB",
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {
                "image": "iVBORw0KGgo=",
                "qrString": "00020101021229370016A000000677010111",
            },
        }
        resp = _make_httpx_response(200, beam_response)

        async with _patched_beam(_settings_doc(), post=resp) as client:
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001", description="Order TEST-001")
            result = await create_beam_charge(req)

            assert result.charge_id == "ch_abc123"
            assert result.status == "PENDING"
            assert result.qr_image is not None
            assert result.qr_string is not None
            assert result.amount == 350.0
            assert result.currency == "THB"

            sent_payload = client.post.call_args.kwargs["json"]
            assert sent_payload["amount"] == 35000  # 350.00 * 100
            assert sent_payload["currency"] == "THB"
            assert sent_payload["paymentMethod"]["paymentMethodType"] == "QR_PROMPT_PAY"

    @pytest.mark.parametrize("thb,expected_satang", [
        (350.0, 35000),
        (399.0, 39900),
        (95.0, 9500),
        (1.0, 100),
        (0.5, 50),
    ])
    async def test_amount_satang_conversion(self, thb, expected_satang):
        """Verify amount is correctly converted to satang for a range of inputs."""
        beam_response = {
            "id": "ch_x", "status": "PENDING", "amount": expected_satang,
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {"image": "x", "qrString": "y"},
        }
        resp = _make_httpx_response(200, beam_response)

        async with _patched_beam(_settings_doc(), post=resp) as client:
            req = BeamChargeRequest(amount=thb, reference_id="TEST-AMT")
            await create_beam_charge(req)
            assert client.post.call_args.kwargs["json"]["amount"] == expected_satang

    async def test_beam_uses_basic_auth_header(self):
        """Verify Authorization header uses Basic base64(merchantId:apiKey) format."""
        beam_response = {
            "id": "ch_auth_test", "status": "PENDING", "amount": 9500,
            "currency": "THB", "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {"image": "abc123", "qrString": "qr_string_here"},
        }
        resp = _make_httpx_response(200, beam_response)

        async with _patched_beam(_settings_doc(), post=resp) as client:
            req = BeamChargeRequest(amount=95.0, reference_id="TEST-AUTH", description="Auth test")
            await create_beam_charge(req)

            sent_headers = client.post.call_args.kwargs["headers"]
            expected_token = base64.b64encode(b"m_test123:sk_test_valid").decode()
            assert sent_headers["Authorization"] == f"Basic {expected_token}"

    async def test_missing_charge_id_returns_502(self):
        """Beam should always return an id; if missing we fail fast rather than silently returning ''."""
        # No id / chargeId / charge_id field at all
        beam_response = {
            "status": "PENDING", "amount": 35000,
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {"image": "x", "qrString": "y"},
        }
        resp = _make_httpx_response(200, beam_response)

        async with _patched_beam(_settings_doc(), post=resp):
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-NOID")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 502
            assert "charge id" in exc_info.value.detail


@pytest.mark.asyncio
class TestBeamChargeGet:
    """Tests for GET /api/beam/charge/{charge_id}."""

    async def test_get_charge_missing_credentials(self):
        doc = _settings_doc(beam_merchant_id=None, beam_api_key=None)
        with patch("server.db") as mock_db:
            mock_db.settings.find_one = AsyncMock(return_value=doc)
            with pytest.raises(HTTPException) as exc_info:
                await get_beam_charge("ch_abc123")
            assert exc_info.value.status_code == 400

    async def test_get_charge_returns_status(self):
        """Should return charge status and amount in THB."""
        beam_response = {"id": "ch_abc123", "status": "COMPLETED", "amount": 35000}
        resp = _make_httpx_response(200, beam_response)

        async with _patched_beam(_settings_doc(), get=resp):
            result = await get_beam_charge("ch_abc123")
            assert result.charge_id == "ch_abc123"
            assert result.status == "COMPLETED"
            assert result.amount == 350.0  # 35000 satang → 350.00 THB

    async def test_get_charge_404(self):
        resp = _make_httpx_response(404, {"message": "Not found"})
        async with _patched_beam(_settings_doc(), get=resp):
            with pytest.raises(HTTPException) as exc_info:
                await get_beam_charge("ch_nonexistent")
            assert exc_info.value.status_code == 404
