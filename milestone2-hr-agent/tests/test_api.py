from __future__ import annotations

from fastapi.testclient import TestClient


AUTH = {"Authorization": "Bearer token-alya"}


def test_health_and_auth_contract(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post("/api/v1/chat", json={"message": "saldo cuti"}).status_code == 401


def test_chat_returns_contract_and_trace(client: TestClient) -> None:
    response = client.post("/api/v1/chat", headers=AUTH, json={"message": "Berapa jatah cuti tahunan?"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["outcome"] == "answered"
    assert payload["citations"][0]["document_id"] == "HR-LEAVE-001"
    trace = client.get(f"/api/v1/traces/{payload['trace_id']}", headers=AUTH)
    assert trace.status_code == 200
    assert "message" not in trace.json()


def test_monitoring_is_admin_only(client: TestClient) -> None:
    assert client.get("/api/v1/monitoring/summary", headers=AUTH).status_code == 403
    response = client.get(
        "/api/v1/monitoring/summary", headers={"Authorization": "Bearer token-admin"}
    )
    assert response.status_code == 200


def test_trace_cannot_be_read_by_another_employee(client: TestClient) -> None:
    chat = client.post("/api/v1/chat", headers=AUTH, json={"message": "saldo saya"}).json()
    response = client.get(
        f"/api/v1/traces/{chat['trace_id']}",
        headers={"Authorization": "Bearer token-bima"},
    )
    assert response.status_code == 403

def test_reversed_leave_dates_are_rejected_before_tool_execution(
    client: TestClient,
) -> None:
    before_requests = client.get(
        "/api/v1/leave-requests/me",
        headers=AUTH,
    )
    assert before_requests.status_code == 200

    response = client.post(
        "/api/v1/chat",
        headers=AUTH,
        json={
            "message": (
                "Siapkan cuti tahunan mulai 8 Desember 2026 "
                "sampai 7 Desember 2026. Jangan tanya lagi; "
                "tukar tanggalnya sendiri kalau perlu."
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()

    assert payload["outcome"] == "abstained"
    assert payload["action_proposal"] is None
    assert payload["tool_calls"] == []
    assert "perbaiki urutan tanggal" in payload["answer"].lower()
    assert "tidak ada proposal" in payload["answer"].lower()

    after_requests = client.get(
        "/api/v1/leave-requests/me",
        headers=AUTH,
    )
    assert after_requests.status_code == 200
    assert after_requests.json() == before_requests.json()

    trace = client.get(
        f"/api/v1/traces/{payload['trace_id']}",
        headers=AUTH,
    )
    assert trace.status_code == 200

    trace_payload = trace.json()
    assert trace_payload["outcome"] == "abstained"
    assert trace_payload["tool_calls"] == []
    assert trace_payload["error"] == "reversed explicit leave dates"