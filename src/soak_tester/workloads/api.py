from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from soak_tester.events import EventWriter, MetricWriter
from soak_tester.openstack_client.resources import (
    owned_metadata,
    resource_record,
    tags_from_metadata,
)

DEFAULT_OPERATIONS = ["list_servers", "list_ports", "list_volumes"]


@dataclass
class OperationResult:
    attempts: int = 0
    successes: int = 0
    errors: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    operation_attempts: dict[str, int] = field(default_factory=dict)
    operation_errors: dict[str, int] = field(default_factory=dict)
    operation_latencies_ms: dict[str, list[float]] = field(default_factory=dict)


class ApiWorkload:
    def __init__(
        self,
        *,
        conn: Any,
        operation_mix: dict[str, int],
        target_ops_per_second: float,
        duration_seconds: int,
        run_id: str,
        project_name: str,
        network_id: str,
        metric_writer: MetricWriter,
        event_writer: EventWriter,
        tracker: Any | None = None,
        create_delete_volume_size_gib: int = 1,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        stop_requested: Callable[[], bool] = lambda: False,
    ):
        self.conn = conn
        self.operations = build_operation_sequence(operation_mix)
        self.target_ops_per_second = target_ops_per_second
        self.duration_seconds = duration_seconds
        self.run_id = run_id
        self.project_name = project_name
        self.network_id = network_id
        self.metric_writer = metric_writer
        self.event_writer = event_writer
        self.tracker = tracker
        self.create_delete_volume_size_gib = create_delete_volume_size_gib
        self.sleep = sleeper
        self.clock = clock
        self.stop_requested = stop_requested
        self.created_index = 0

    def run(self) -> OperationResult:
        result = OperationResult()
        self.event_writer.write(level="info", plane="api", event="api_started")
        start = self.clock()
        deadline = start + self.duration_seconds
        max_attempts = max(1, math.ceil(self.target_ops_per_second * self.duration_seconds))
        interval = 1 / self.target_ops_per_second if self.target_ops_per_second > 0 else 0
        next_attempt = start
        while self.clock() < deadline and result.attempts < max_attempts:
            if self.stop_requested():
                raise InterruptedError("Stop requested")
            if interval > 0:
                wait = next_attempt - self.clock()
                while wait > 0 and self.clock() < deadline:
                    if self.stop_requested():
                        raise InterruptedError("Stop requested")
                    self.sleep(min(wait, 1, deadline - self.clock()))
                    wait = next_attempt - self.clock()
                next_attempt += interval
            if self.stop_requested():
                raise InterruptedError("Stop requested")
            if self.clock() >= deadline:
                break
            operation = self.operations[result.attempts % len(self.operations)]
            self._run_operation(operation, result)
        # The offered rate covers the requested run window, including the tail
        # after the last scheduled operation; do not inflate low-rate results.
        while (remaining := deadline - self.clock()) > 0:
            if self.stop_requested():
                raise InterruptedError("Stop requested")
            self.sleep(min(remaining, 1))
        if self.stop_requested():
            raise InterruptedError("Stop requested")
        elapsed = max(self.clock() - start, 0.001)
        self._write_metrics(result, elapsed)
        self.event_writer.write(
            level="info",
            plane="api",
            event="api_completed",
            details={
                "attempts": result.attempts,
                "successes": result.successes,
                "errors": result.errors,
                "elapsed_seconds": elapsed,
            },
        )
        return result

    def _run_operation(self, operation: str, result: OperationResult) -> None:
        op_start = self.clock()
        result.attempts += 1
        result.operation_attempts[operation] = result.operation_attempts.get(operation, 0) + 1
        try:
            self._dispatch(operation)
            result.successes += 1
        except Exception as exc:
            result.errors += 1
            result.operation_errors[operation] = result.operation_errors.get(operation, 0) + 1
            if result.errors <= 20:
                self.event_writer.write(
                    level="warning",
                    plane="api",
                    event="api_operation_error",
                    details={"operation": operation, "error": str(exc)},
                )
        latency = max((self.clock() - op_start) * 1000, 0)
        result.latencies_ms.append(latency)
        result.operation_latencies_ms.setdefault(operation, []).append(latency)

    def _dispatch(self, operation: str) -> None:
        if operation == "list_servers":
            list(self.conn.compute.servers(details=True))
        elif operation == "list_ports":
            list(self.conn.network.ports())
        elif operation == "list_volumes":
            list(self.conn.block_storage.volumes())
        elif operation == "show_server":
            server = _first_or_none(self.conn.compute.servers(details=True))
            if server is None:
                raise RuntimeError("no server available for show_server")
            self.conn.compute.get_server(server.id)
        elif operation == "show_volume":
            volume = _first_or_none(self.conn.block_storage.volumes())
            if volume is None:
                raise RuntimeError("no volume available for show_volume")
            self.conn.block_storage.get_volume(volume.id)
        elif operation == "create_delete_ports":
            self._create_delete_port()
        elif operation == "create_delete_volumes":
            self._create_delete_volume()
        else:
            raise RuntimeError(f"unsupported API operation: {operation}")

    def _create_delete_port(self) -> None:
        name = f"soak-api-{self.run_id}-{self.created_index:06d}"
        self.created_index += 1
        metadata = owned_metadata(self.run_id, "api", "port", self.project_name)
        port = self.conn.network.create_port(name=name, network_id=self.network_id)
        self._record_resource("port", port.id, port.name, metadata)
        self._tag_network_resource(port, metadata)
        self.conn.network.delete_port(port.id, ignore_missing=True)

    def _create_delete_volume(self) -> None:
        name = f"soak-api-{self.run_id}-volume-{self.created_index:06d}"
        self.created_index += 1
        metadata = owned_metadata(self.run_id, "api", "volume", self.project_name)
        volume = self.conn.block_storage.create_volume(
            name=name,
            size=self.create_delete_volume_size_gib,
            metadata=metadata,
        )
        self._record_resource("volume", volume.id, volume.name, metadata)
        if hasattr(self.conn.block_storage, "wait_for_status"):
            volume = self.conn.block_storage.wait_for_status(
                volume,
                status="available",
                failures=["error"],
                wait=120,
            )
        self.conn.block_storage.delete_volume(volume.id, ignore_missing=True)

    def _tag_network_resource(self, resource: Any, metadata: dict[str, str]) -> None:
        if not hasattr(self.conn.network, "set_tags"):
            return
        try:
            self.conn.network.set_tags(resource, tags_from_metadata(metadata))
        except Exception:
            return

    def _record_resource(
        self,
        resource_type: str,
        resource_id: str,
        name: str,
        metadata: dict[str, str],
    ) -> None:
        if self.tracker is None:
            return
        self.tracker.add(
            resource_record(
                resource_type,
                resource_id,
                name,
                plane="api",
                role=resource_type,
                metadata=metadata,
            )
        )

    def _write_metrics(self, result: OperationResult, elapsed: float) -> None:
        attempts = max(result.attempts, 1)
        self._metric("api.ops_per_second", result.attempts / elapsed, "ops/s")
        self._metric("api.success_ops_per_second", result.successes / elapsed, "ops/s")
        self._metric("api.errors", result.errors, "count")
        self._metric("api.error_rate", result.errors / attempts, "ratio")
        self._metric("api.latency_p50_ms", percentile(result.latencies_ms, 50), "ms")
        self._metric("api.latency_p95_ms", percentile(result.latencies_ms, 95), "ms")
        self._metric("api.latency_p99_ms", percentile(result.latencies_ms, 99), "ms")
        for operation, count in sorted(result.operation_attempts.items()):
            tags = {"operation": operation}
            errors = result.operation_errors.get(operation, 0)
            self._metric("api.operation.count", count, "count", tags=tags)
            self._metric("api.operation.errors", errors, "count", tags=tags)
            self._metric(
                "api.operation.latency_p95_ms",
                percentile(result.operation_latencies_ms.get(operation, []), 95),
                "ms",
                tags=tags,
            )

    def _metric(
        self,
        metric: str,
        value: float,
        unit: str,
        tags: dict[str, str] | None = None,
    ) -> None:
        self.metric_writer.write(
            plane="api",
            worker_id="local",
            metric=metric,
            value=float(value),
            unit=unit,
            tags=tags or {},
        )


def build_operation_sequence(operation_mix: dict[str, int]) -> list[str]:
    if not operation_mix:
        return list(DEFAULT_OPERATIONS)
    sequence: list[str] = []
    for operation, weight in operation_mix.items():
        sequence.extend([operation] * int(weight))
    return sequence


def percentile(values: list[float], pct: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil((pct / 100) * len(ordered)) - 1)
    return float(ordered[index])


def _first_or_none(values: Any) -> Any | None:
    for value in values:
        return value
    return None


def run_api_sample(conn: Any, *, seconds: int, target_ops_per_second: float) -> dict[str, float]:
    result = OperationResult()
    start = time.monotonic()
    deadline = start + seconds
    while time.monotonic() < deadline:
        op_start = time.monotonic()
        try:
            list(conn.compute.servers())
            list(conn.network.ports())
            list(conn.block_storage.volumes())
            result.successes += 3
        except Exception:
            result.errors += 1
        result.attempts += 3
        result.latencies_ms.append((time.monotonic() - op_start) * 1000)
        if target_ops_per_second > 0:
            time.sleep(min(1 / target_ops_per_second, 1))
    elapsed = max(time.monotonic() - start, 0.001)
    return {
        "requested_ops_per_second": target_ops_per_second,
        "achieved_ops_per_second": result.attempts / elapsed,
        "errors": result.errors,
        "latency_p50_seconds": percentile(result.latencies_ms, 50) / 1000,
    }
