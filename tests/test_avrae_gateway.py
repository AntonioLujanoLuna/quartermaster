from __future__ import annotations

import asyncio
import json
import unittest

from integrations.avrae.quartermaster_adapter import (
    QuartermasterStatusAdapter,
    RequestRejected,
)

from quartermaster.avrae_gateway import (
    AVRAE_STATUS_PROTOCOL,
    NONCE_HEADER,
    PROTOCOL_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    HttpAvraeGateway,
    encode_wire_request,
    signature_for,
    verify_signature,
)
from quartermaster.integration import ProviderIntegrationError, ProviderRequest, ProviderTimeout


class AvraeGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.provider_request = ProviderRequest(
            operation_id="operation-1",
            provider="avrae",
            operation_kind="status",
            actor_id="actor-1",
            guild_id="guild-1",
            channel_id="channel-1",
            session_id="session-1",
            provider_reference="channel:channel-1",
            correlation_id="qm:operation-1",
            payload={"source": "test"},
        )

    def test_gateway_signs_a_deterministic_status_envelope(self) -> None:
        captured = {}

        def transport(request, timeout):
            captured["request"] = request
            captured["timeout"] = timeout
            return json.dumps(
                {
                    "status": "COMMITTED",
                    "provider_reference": "channel:channel-1",
                    "correlation_id": "qm:operation-1",
                    "provider_version": "test-provider",
                    "payload": {"active": True},
                }
            ).encode()

        gateway = HttpAvraeGateway(
            "https://avrae.example/status",
            "shared-secret",
            timeout_seconds=1.7,
            transport=transport,
            clock=lambda: 1_000.9,
        )
        result = gateway.execute(self.provider_request)

        request = captured["request"]
        body = request.data
        timestamp = request.headers["X-quartermaster-timestamp"]
        nonce = request.headers["X-quartermaster-nonce"]
        self.assertEqual(result.status, "COMMITTED")
        self.assertEqual(captured["timeout"], 1.7)
        self.assertEqual(request.full_url, "https://avrae.example/status")
        self.assertEqual(request.headers["X-quartermaster-protocol"], AVRAE_STATUS_PROTOCOL)
        self.assertEqual(
            request.headers["X-quartermaster-signature"],
            signature_for(secret="shared-secret", timestamp=timestamp, nonce=nonce, body=body),
        )
        verify_signature(
            secret="shared-secret",
            timestamp=timestamp,
            nonce=nonce,
            signature=request.headers["X-quartermaster-signature"],
            body=body,
            now=1_001,
        )
        self.assertEqual(json.loads(body)["operation_kind"], "status")

    def test_gateway_refuses_a_state_changing_operation_before_network(self) -> None:
        request = self.provider_request.__class__(**{**self.provider_request.__dict__, "operation_kind": "attack"})
        gateway = HttpAvraeGateway("https://avrae.example/status", "shared-secret", transport=lambda *_: b"{}")
        with self.assertRaises(ProviderIntegrationError):
            gateway.execute(request)

    def test_gateway_preserves_unknown_when_transport_times_out(self) -> None:
        def transport(_request, _timeout):
            raise ProviderTimeout("Avrae did not confirm the status")

        gateway = HttpAvraeGateway("https://avrae.example/status", "shared-secret", transport=transport)
        with self.assertRaises(ProviderTimeout):
            gateway.execute(self.provider_request)

    def test_response_metadata_and_payload_are_validated(self) -> None:
        gateway = HttpAvraeGateway(
            "https://avrae.example/status",
            "shared-secret",
            transport=lambda *_: b'{"status":"COMMITTED","payload":[]}',
        )
        with self.assertRaises(ProviderIntegrationError):
            gateway.execute(self.provider_request)


class AvraeAdapterTests(unittest.TestCase):
    def _request(self) -> ProviderRequest:
        return ProviderRequest(
            operation_id="operation-1",
            provider="avrae",
            operation_kind="status",
            actor_id="actor-1",
            guild_id="guild-1",
            channel_id="channel-1",
            session_id="session-1",
            provider_reference="channel:channel-1",
            correlation_id="qm:operation-1",
            payload={"source": "test"},
        )

    def _signed(self, request: ProviderRequest) -> tuple[bytes, dict[str, str]]:
        body = encode_wire_request(request)
        timestamp = "1000"
        nonce = "nonce-1"
        return body, {
            PROTOCOL_HEADER: AVRAE_STATUS_PROTOCOL,
            TIMESTAMP_HEADER: timestamp,
            NONCE_HEADER: nonce,
            SIGNATURE_HEADER: signature_for(
                secret="shared-secret", timestamp=timestamp, nonce=nonce, body=body
            ),
        }

    def test_adapter_validates_and_dispatches_the_signed_request(self) -> None:
        seen = []

        class Provider:
            def combat_status(self, request):
                seen.append(request)
                return {"active": True, "round": 2}

        adapter = QuartermasterStatusAdapter("shared-secret", "guild-1", Provider())
        body, headers = self._signed(self._request())
        result = asyncio.run(adapter.handle(body, headers=headers, now=1_000))

        self.assertEqual(result["status"], "COMMITTED")
        self.assertEqual(result["payload"], {"active": True, "round": 2})
        self.assertEqual(seen[0]["actor_id"], "actor-1")
        self.assertEqual(seen[0]["channel_id"], "channel-1")

    def test_adapter_rejects_a_replayed_nonce(self) -> None:
        class Provider:
            def combat_status(self, _request):
                return {"active": False}

        adapter = QuartermasterStatusAdapter("shared-secret", "guild-1", Provider())
        body, headers = self._signed(self._request())
        self.assertEqual(asyncio.run(adapter.handle(body, headers=headers, now=1_000))["status"], "COMMITTED")
        with self.assertRaises(RequestRejected):
            asyncio.run(adapter.handle(body, headers=headers, now=1_000))

    def test_adapter_rejects_the_wrong_guild_before_native_dispatch(self) -> None:
        class Provider:
            def combat_status(self, _request):
                raise AssertionError("native provider must not be called")

        adapter = QuartermasterStatusAdapter("shared-secret", "other-guild", Provider())
        body, headers = self._signed(self._request())
        with self.assertRaises(RequestRejected):
            asyncio.run(adapter.handle(body, headers=headers, now=1_000))

    def test_adapter_converts_a_native_read_failure_to_a_known_failed_result(self) -> None:
        class Provider:
            def combat_status(self, _request):
                raise RuntimeError("native model unavailable")

        adapter = QuartermasterStatusAdapter("shared-secret", "guild-1", Provider())
        body, headers = self._signed(self._request())
        result = asyncio.run(adapter.handle(body, headers=headers, now=1_000))
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "native Avrae status is unavailable")

    def test_signature_rejects_stale_requests(self) -> None:
        body = encode_wire_request(self._request())
        signature = signature_for(secret="shared-secret", timestamp="1000", nonce="nonce", body=body)
        with self.assertRaises(ProviderIntegrationError):
            verify_signature(
                secret="shared-secret",
                timestamp="1000",
                nonce="nonce",
                signature=signature,
                body=body,
                now=1_100,
            )
