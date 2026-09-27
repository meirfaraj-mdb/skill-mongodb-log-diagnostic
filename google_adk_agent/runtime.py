"""Runtime configuration for the ADK deployment.

No secret value is packaged in the agent.  The only value constructed here is the
Secret Manager resource name; workload identity reads its value at invocation.
"""
from __future__ import annotations

import os


def configure_gcp_runtime() -> None:
    """Set non-secret defaults required by the cloud-neutral pipeline."""
    os.environ.setdefault("CLOUD_PROVIDER", "gcp")
    if not os.environ.get("GOOGLE_CLOUD_PROJECT"):
        import google.auth
        _, project = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        if project:
            os.environ["GOOGLE_CLOUD_PROJECT"] = project
    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    secret_name = os.environ.get("ATLAS_SECRET_NAME", "atlas-log-agent")
    if project and not os.environ.get("ATLAS_SECRET_ID"):
        os.environ["ATLAS_SECRET_ID"] = f"projects/{project}/secrets/{secret_name}/versions/latest"
