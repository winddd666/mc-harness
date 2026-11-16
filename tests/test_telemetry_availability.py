from mc_agent.console.server import _build_telemetry_payload


def test_unknown_or_disconnected_inventory_is_not_an_empty_bag():
    for status, inventory in [(None, None), ({"ready": False}, {"items": []}),
                              ({"ready": True}, {"ok": False, "error": {"code": "not_connected"}}),
                              ({"ready": True}, None)]:
        payload = _build_telemetry_payload(status, inventory)
        assert payload["inventory_available"] is False
        assert payload["inventory_total"] is None


def test_observed_empty_inventory_differs_from_unavailable_inventory():
    payload = _build_telemetry_payload({"ready": True}, {"items": []})
    assert payload["inventory_available"] is True
    assert payload["inventory_total"] == 0
