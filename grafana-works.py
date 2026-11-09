#!/usr/bin/env python3
"""
Smoke test for the OTLP metrics endpoint: posts one dual-stack test metric.

Usage:
    GRAFANA_API_KEY=... python grafana-works.py [URL]

URL defaults to grafana_url from config.yaml.
"""

import os
import sys
import time

import requests

from config import load_config


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else load_config().get("grafana_url")
    if not url:
        print("No URL given and grafana_url not set in config.yaml", file=sys.stderr)
        return 2

    now_ns = str(time.time_ns())
    payload = {"resourceMetrics": [{"scopeMetrics": [{"metrics": [{
        "name": "rootviz_smoke_test",
        "unit": "1",
        "description": "rootviz connectivity test",
        "gauge": {"dataPoints": [
            {"asInt": 123, "timeUnixNano": now_ns,
             "attributes": [{"key": "protocol", "value": {"stringValue": "v4"}}]},
            {"asInt": 456, "timeUnixNano": now_ns,
             "attributes": [{"key": "protocol", "value": {"stringValue": "v6"}}]},
        ]},
    }]}]}]}

    headers = {"Content-Type": "application/json"}
    if os.environ.get("GRAFANA_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['GRAFANA_API_KEY']}"

    response = requests.post(url, headers=headers, json=payload, timeout=30)
    print("Response Status:", response.status_code)
    print("Response Body:", response.text)
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())
