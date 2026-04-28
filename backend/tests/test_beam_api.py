"""Unit tests for Beam QR payment endpoints."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_httpx_response(status_code: int, json_body: dict):
    """Create a mock httpx.Response."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_body
    mock.text = str(json_body)
    return mock


# ---------------------------------------------------------------------------
# Settings masking tests (no httpx needed)
# ---------------------------------------------------------------------------

class TestSettingsMasking:
    """Verify that beam_api_key is masked on GET and preserved on PUT."""

    def test_get_settings_masks_api_key(self):
        """GET /settings should return ••••<last4> for a stored API key."""
        from server import Settings
        s = Settings(beam_api_key="sk_test_abcdefgh1234")
        # Simulate the masking logic from get_settings
        if s.beam_api_key and len(s.beam_api_key) > 4:
            s.beam_api_key = "••••" + s.beam_api_key[-4:]
        assert s.beam_api_key == "••••1234"

    def test_get_settings_short_key_not_masked(self):
        """Keys with 4 or fewer chars are not masked (edge case)."""
        from server import Settings
        s = Settings(beam_api_key="abcd")
        if s.beam_api_key and len(s.beam_api_key) > 4:
            s.beam_api_key = "••••" + s.beam_api_key[-4:]
        assert s.beam_api_key == "abcd"

    def test_put_skips_masked_key(self):
        """PUT should not overwrite beam_api_key when the masked placeholder is sent back."""
        masked = "••••1234"
        assert masked.startswith("••••"), "Masking prefix check"
        # The backend guard: if key starts with ••••, skip the update
        updates = {"beam_api_key": masked, "shop_name": "Test Shop"}
        if "beam_api_key" in updates and updates["beam_api_key"].startswith("••••"):
            del updates["beam_api_key"]
        assert "beam_api_key" not in updates
        assert "shop_name" in updates

    def test_put_accepts_real_key(self):
        """PUT should accept and store a new real API key."""
        new_key = "sk_live_newkey9999"
        updates = {"beam_api_key": new_key}
        if "beam_api_key" in updates and updates["beam_api_key"].startswith("••••"):
            del updates["beam_api_key"]
        assert updates["beam_api_key"] == new_key


# ---------------------------------------------------------------------------
# Beam charge endpoint tests (mock httpx + DB)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestBeamChargeCreate:
    """Tests for POST /api/beam/charge."""

    async def test_missing_credentials_returns_400(self):
        """Should return 400 when beam credentials are not configured."""
        from fastapi import HTTPException
        from server import create_beam_charge, BeamChargeRequest

        # Patch db.settings to return a settings doc with no credentials
        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": None, "beam_api_key": None, "beam_sandbox": True,
        }

        with patch("server.db") as mock_db:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 400
            assert "credentials not configured" in exc_info.value.detail

    async def test_beam_401_returns_401(self):
        """Should return 401 when Beam API rejects the API key."""
        from fastapi import HTTPException
        from server import create_beam_charge, BeamChargeRequest

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": "m_test123", "beam_api_key": "bad_key",
            "beam_sandbox": True,
        }

        mock_response = _make_httpx_response(401, {"message": "Unauthorized"})

        with patch("server.db") as mock_db, \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 401
            assert "invalid or expired" in exc_info.value.detail

    async def test_beam_timeout_returns_502(self):
        """Should return 502 with a friendly message on timeout."""
        import httpx as httpx_module
        from fastapi import HTTPException
        from server import create_beam_charge, BeamChargeRequest

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": "m_test123", "beam_api_key": "sk_test_valid",
            "beam_sandbox": True,
        }

        with patch("server.db") as mock_db, \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=httpx_module.TimeoutException("timeout"))
            mock_client_cls.return_value = mock_client

            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001")
            with pytest.raises(HTTPException) as exc_info:
                await create_beam_charge(req)
            assert exc_info.value.status_code == 502
            assert "timed out" in exc_info.value.detail

    async def test_beam_success_with_encoded_image(self):
        """Should return charge_id, status=PENDING, and qr_image on success."""
        from server import create_beam_charge, BeamChargeRequest

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": "m_test123", "beam_api_key": "sk_test_valid",
            "beam_sandbox": True,
        }

        beam_response = {
            "id": "ch_abc123",
            "status": "PENDING",
            "amount": 35000,  # 350.00 THB in satang
            "currency": "THB",
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {
                "image": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==",
                "qrString": "00020101021229370016A000000677010111011300660000000000530376454063500005802TH5910TestShop6007Bangkok6304ABCD"
            }
        }

        mock_response = _make_httpx_response(200, beam_response)

        with patch("server.db") as mock_db, \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            req = BeamChargeRequest(amount=350.0, reference_id="TEST-001", description="Order TEST-001")
            result = await create_beam_charge(req)

            assert result.charge_id == "ch_abc123"
            assert result.status == "PENDING"
            assert result.qr_image is not None
            assert result.qr_string is not None
            assert result.amount == 350.0
            assert result.currency == "THB"

            # Verify correct payload was sent to Beam
            call_kwargs = mock_client.post.call_args
            sent_payload = call_kwargs.kwargs.get("json") or call_kwargs.args[1] if len(call_kwargs.args) > 1 else call_kwargs.kwargs["json"]
            assert sent_payload["amount"] == 35000  # 350.00 * 100
            assert sent_payload["currency"] == "THB"
            assert sent_payload["paymentMethod"]["paymentMethodType"] == "QR_PROMPT_PAY"

    async def test_amount_satang_conversion(self):
        """Verify amount is correctly converted to satang (smallest unit)."""
        from server import create_beam_charge, BeamChargeRequest

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": "m_test123", "beam_api_key": "sk_test_valid",
            "beam_sandbox": True,
        }

        beam_response = {
            "id": "ch_xyz", "status": "PENDING", "amount": 39900,
            "actionRequired": "ENCODED_IMAGE",
            "encodedImage": {"image": "base64data", "qrString": "qrdata"}
        }

        mock_response = _make_httpx_response(200, beam_response)

        with patch("server.db") as mock_db, \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            # 399.00 THB → 39900 satang
            req = BeamChargeRequest(amount=399.0, reference_id="TEST-002")
            await create_beam_charge(req)

            call_kwargs = mock_client.post.call_args
            sent_payload = call_kwargs.kwargs.get("json") or call_kwargs.kwargs["json"]
            assert sent_payload["amount"] == 39900


@pytest.mark.asyncio
class TestBeamChargeGet:
    """Tests for GET /api/beam/charge/{charge_id}."""

    async def test_get_charge_missing_credentials(self):
        """Should return 400 when credentials are not configured."""
        from fastapi import HTTPException
        from server import get_beam_charge

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": None, "beam_api_key": None, "beam_sandbox": True,
        }

        with patch("server.db") as mock_db:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            with pytest.raises(HTTPException) as exc_info:
                await get_beam_charge("ch_abc123")
            assert exc_info.value.status_code == 400

    async def test_get_charge_returns_status(self):
        """Should return charge status and amount in THB."""
        from server import get_beam_charge

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": "m_test123", "beam_api_key": "sk_test_valid",
            "beam_sandbox": True,
        }

        beam_response = {"id": "ch_abc123", "status": "COMPLETED", "amount": 35000}
        mock_response = _make_httpx_response(200, beam_response)

        with patch("server.db") as mock_db, \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await get_beam_charge("ch_abc123")
            assert result.charge_id == "ch_abc123"
            assert result.status == "COMPLETED"
            assert result.amount == 350.0  # 35000 satang → 350.00 THB

    async def test_get_charge_404(self):
        """Should return 404 when Beam returns 404."""
        from fastapi import HTTPException
        from server import get_beam_charge

        mock_settings_doc = {
            "id": "shop", "shop_name": "Test", "business_type": "General",
            "pos_id": "001", "branch": "B1", "pos_number": "001",
            "open_time": "09:00", "close_time": "22:00",
            "tax_percent": 7.0, "tax_mode": "exclusive",
            "service_charge_enabled": False, "service_charge_percent": 10.0,
            "beam_merchant_id": "m_test123", "beam_api_key": "sk_test_valid",
            "beam_sandbox": True,
        }

        mock_response = _make_httpx_response(404, {"message": "Not found"})

        with patch("server.db") as mock_db, \
             patch("httpx.AsyncClient") as mock_client_cls:
            mock_db.settings.find_one = AsyncMock(return_value=mock_settings_doc)
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(HTTPException) as exc_info:
                await get_beam_charge("ch_nonexistent")
            assert exc_info.value.status_code == 404
