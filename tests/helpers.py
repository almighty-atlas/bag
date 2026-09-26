from typing import Any

from bag.worker import Worker
from fastapi.testclient import TestClient


def drain(worker: Worker, limit: int = 100) -> int:
    count = 0
    while worker.run_once():
        count += 1
        assert count <= limit, "queue did not drain"
    return count


def runs(client: TestClient, headers: dict[str, str], item_id: str) -> dict[str, dict[str, Any]]:
    response = client.get(f"/api/v1/items/{item_id}/processing", headers=headers)
    assert response.status_code == 200, response.text
    return {row["processor"]: row for row in response.json()}
