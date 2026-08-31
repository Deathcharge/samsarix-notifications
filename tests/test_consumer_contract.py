# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_order_consumer_contract_across_worker_process_restarts(tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "examples" / "durable_outbox.py"
    completed = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert json.loads(completed.stdout) == {
        "contract": "order_confirmed_v1",
        "rollback_verified": True,
        "worker_processes": 2,
        "http_attempts": 2,
        "accepted_events": 1,
        "durable_status": "delivered",
        "duplicate_created": False,
    }
