import json
import threading
import urllib.request
import urllib.error
import pytest

from soak_tester.run.controller import controller_cloud_init


def test_controller_status_keeps_every_worker_and_latest_heartbeat(tmp_path):
    script = controller_cloud_init(token="test-secret")
    source = script.split("<<'PY'\n", 1)[1].split("\nPY", 1)[0]
    source = source.split("server = ThreadingHTTPServer", 1)[0]
    source = source.replace("/var/lib/soak-tester", str(tmp_path))
    namespace = {}
    exec(compile(source, "controller", "exec"), namespace)
    server = namespace["ThreadingHTTPServer"](("127.0.0.1", 0), namespace["Handler"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as forbidden:
            urllib.request.urlopen(url + "/control", timeout=5)
        assert forbidden.value.code == 403
        request = urllib.request.Request(
            url + "/control",
            data=json.dumps({"duration_seconds": 60}).encode(),
            headers={"authorization": "Bearer test-secret"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            control = json.load(response)
        assert control["ends_at"] - control["started_at"] == pytest.approx(60, abs=0.01)
        with pytest.raises(urllib.error.HTTPError) as repeated:
            urllib.request.urlopen(request, timeout=5)
        assert repeated.value.code == 409
        for index in range(251):
            payload = {"worker_id": f"worker-{index % 250}", "status": index}
            request = urllib.request.Request(
                url + "/heartbeat", data=json.dumps(payload).encode(), method="POST"
            )
            with urllib.request.urlopen(request, timeout=5) as response:
                assert response.status == 200
        (tmp_path / "metrics.jsonl").write_text(
            "".join(json.dumps({"value": i}) + "\n" for i in range(100001))
        )
        with urllib.request.urlopen(url + "/metrics", timeout=5) as response:
            metrics = json.load(response)["metrics"]
        assert len(metrics) == 100001
        assert metrics[0]["value"] == 0
        assert metrics[-1]["value"] == 100000
        (tmp_path / "events.jsonl").write_text(
            "".join(
                json.dumps({"event": "failed" if i == 0 else "done"}) + "\n" for i in range(501)
            )
        )
        with urllib.request.urlopen(url + "/events", timeout=5) as response:
            retained_events = json.load(response)["events"]
        assert len(retained_events) == 501
        assert retained_events[0]["event"] == "failed"
        with urllib.request.urlopen(url + "/status", timeout=5) as response:
            heartbeats = json.load(response)["heartbeats"]
        assert len(heartbeats) == 250
        assert next(h for h in heartbeats if h["worker_id"] == "worker-0")["status"] == 250
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
