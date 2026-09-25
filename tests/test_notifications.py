from fastapi.testclient import TestClient

from app.main import app
from app.worker import process_next_document
from test_portal import auth_headers, clean_state, records, upload  # noqa: F401


def test_status_changes_create_scoped_readable_notifications(records):
    with TestClient(app) as client:
        admin = auth_headers(client, "admin@example.com", records["password"])
        customer = auth_headers(client, "client@example.com", records["password"])
        accountant = auth_headers(client, "accountant@example.com", records["password"])

        created = client.post(
            "/api/v1/collection-requests",
            headers={**admin, "Idempotency-Key": "notification-create"},
            json={
                "client_id": str(records["first"]),
                "period": "2026-10-01",
                "due_at": "2026-10-25T23:59:00+08:00",
                "assignee_id": str(records["admin"]),
                "requirements": [
                    {
                        "type": "BANK_STATEMENT",
                        "title": "Bank statement",
                        "required": True,
                        "criteria": {},
                    }
                ],
            },
        ).json()
        published = client.post(
            f"/api/v1/collection-requests/{created['id']}/publish",
            headers={**admin, "Idempotency-Key": "notification-publish"},
            json={"version": created["version"]},
        )
        assert published.status_code == 200

        customer_list = client.get("/api/v1/notifications", headers=customer)
        assert customer_list.status_code == 200
        assert customer_list.json()["unread_count"] == 1
        notice = customer_list.json()["items"][0]
        assert notice["event_type"] == "PUBLISHED"
        assert notice["client_name"] == "First Client"
        assert notice["period"] == "2026-10-01"

        assert client.get("/api/v1/notifications", headers=admin).json()["total"] == 0
        assert client.get("/api/v1/notifications", headers=accountant).json()["total"] == 0
        assert client.post(
            f"/api/v1/notifications/{notice['id']}/read", headers=admin
        ).status_code == 404

        marked = client.post(
            f"/api/v1/notifications/{notice['id']}/read", headers=customer
        )
        assert marked.status_code == 200 and marked.json()["read_at"]
        assert client.get(
            "/api/v1/notifications?unread_only=true", headers=customer
        ).json()["total"] == 0

        assert upload(
            client, customer, records["request"], records["required"]
        ).status_code == 202
        assert process_next_document()
        submitted = client.post(
            f"/api/v1/portal/collection-requests/{records['request']}/submit",
            headers=customer,
        )
        assert submitted.status_code == 200
        admin_list = client.get("/api/v1/notifications", headers=admin).json()
        assert admin_list["unread_count"] == 1
        assert admin_list["items"][0]["event_type"] == "SUBMITTED"
        assert admin_list["items"][0]["payload"] == {"round_no": 1}

        assert client.post("/api/v1/notifications/read-all", headers=admin).status_code == 204
        assert client.get("/api/v1/notifications", headers=admin).json()["unread_count"] == 0
