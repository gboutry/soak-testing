from soak_tester.cleanup import cleanup_order, dry_run_cleanup


def test_cleanup_dry_run_uses_safe_order():
    resources = {
        "resources": [
            {"type": "project", "id": "project-1", "name": "project"},
            {"type": "server", "id": "server-1", "name": "server"},
            {"type": "volume", "id": "volume-1", "name": "volume"},
            {"type": "floating_ip", "id": "fip-1", "name": "203.0.113.10"},
            {"type": "network", "id": "network-1", "name": "network"},
        ]
    }

    actions = dry_run_cleanup(resources)

    assert [action["type"] for action in actions] == [
        "server",
        "volume",
        "floating_ip",
        "network",
        "project",
    ]
    assert cleanup_order("server") < cleanup_order("project")
