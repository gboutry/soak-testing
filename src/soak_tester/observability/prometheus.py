from __future__ import annotations

import os
from typing import Any

import requests

from soak_tester.config import PrometheusConfig
from soak_tester.util import duration_seconds


class PrometheusClient:
    def __init__(self, config: PrometheusConfig):
        self.config = config
        self.session = requests.Session()
        if config.auth.type == "bearer" and config.auth.token_file:
            token = open(config.auth.token_file, encoding="utf-8").read().strip()
            self.session.headers["authorization"] = f"Bearer {token}"
        elif config.auth.type == "basic" and config.auth.username and config.auth.password_env:
            self.session.auth = (config.auth.username, os.environ.get(config.auth.password_env, ""))

    @property
    def verify(self) -> bool | str:
        return self.config.tls.ca_file or self.config.tls.verify

    @property
    def timeout(self) -> int:
        return duration_seconds(self.config.timeout)

    def query(self, query: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.config.url.rstrip('/')}/api/v1/query",
            params={"query": query},
            timeout=self.timeout,
            verify=self.verify,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload)
        return payload

    def query_range(self, query: str, *, start: str, end: str, step: str) -> dict[str, Any]:
        response = self.session.get(
            f"{self.config.url.rstrip('/')}/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
            timeout=self.timeout,
            verify=self.verify,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(payload)
        return payload
