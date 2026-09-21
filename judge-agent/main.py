"""main.py

100% Dynamic A2A Server for Cloud Run.
- Zero hardcoded URLs or project IDs.
- Auto-detects public Cloud Run URL via GCP Metadata Server on startup.
- Generates runtime AgentCard dynamically on boot matching the live environment.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from urllib.parse import urlparse
import uvicorn
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from agent import judge_agent

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

PORT = int(os.getenv("PORT", "8080"))


def get_cloud_run_url() -> str:
    """Attempts to auto-discover the Cloud Run service URL at runtime."""
    # 1. Check if user explicitly passed it via env var
    explicit_url = os.getenv("A2A_PUBLIC_URL")
    if explicit_url:
        return explicit_url.rstrip("/")

    # 2. Query the Google Cloud Metadata Server for the service URL
    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-url",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            detected_url = resp.read().decode("utf-8").strip()
            if detected_url:
                logger.info(f"Auto-detected Cloud Run URL via metadata server: {detected_url}")
                return detected_url.rstrip("/")
    except Exception:
        pass

    # 3. Fallback for local testing / development
    return f"http://localhost:{PORT}"


canonical_origin = get_cloud_run_url()

card_payload = {
    "name": judge_agent.name,
    "description": "Remote Judge Agent evaluating query efficiency, semantic alignment, and accuracy over A2A.",
    "url": canonical_origin,
    "supportedInterfaces": [
        {
            "url": canonical_origin,
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "version": "0.0.1",
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": judge_agent.name,
            "name": "governance_evaluation",
            "description": "Evaluates SQL efficiency and analytical accuracy.",
            "tags": ["llm", "governance", "sql"],
        }
    ],
}

runtime_card_path = "/tmp/dynamic_agent_card.json"
with open(runtime_card_path, "w", encoding="utf-8") as f:
    json.dump(card_payload, f)

a2a_app = to_a2a(
    agent=judge_agent,
    agent_card=runtime_card_path,
)

if __name__ == "__main__":
    uvicorn.run(a2a_app, host="0.0.0.0", port=PORT)