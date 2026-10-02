"""Discovery must expose retained values without presenting them as live."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import harness


def test_discovery_preserves_connection_metadata_and_retained_co2():
    C = harness.load()
    sensors = [{"id": "offline", "name": "Offline", "isConnected": False,
                "airQuality": {"co2": 500}},
               {"id": "online", "name": "Online", "isConnected": True,
                "airQuality": {"co2": 600}},
               {"id": "legacy", "name": "Legacy", "airQuality": {"co2": 700}}]
    class Protect:
        def sensors(self):
            return sensors
    C._clients = lambda: (Protect(), None)
    response = C.app.test_client().post("/api/protect/sensors", json={})
    assert response.status_code == 200
    found = response.get_json()["sensors"]
    assert found[0]["isConnected"] is False
    assert found[0]["co2"] == 500
    assert found[1]["isConnected"] is True
    assert found[2]["isConnected"] is None


if __name__ == "__main__":
    sys.exit(harness.run(dict(globals())))
