from soak_tester.run.provision import _network_sender_legacy_peers


def test_network_senders_do_not_use_legacy_peers_when_flows_are_planned():
    assert _network_sender_legacy_peers(["10.0.0.10"], planned_flows=True) == []


def test_network_senders_keep_legacy_peers_without_planned_flows():
    assert _network_sender_legacy_peers(["10.0.0.10"], planned_flows=False) == [
        "10.0.0.10"
    ]
