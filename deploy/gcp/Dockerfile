# Self-contained build: the diagnostic skill is vendored in skills/mongodb-log-diagnostic.
# Build from project root: docker build -f deploy/gcp/Dockerfile -t mongodb-log-diagnostic-agent:gcp .
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements-gcp.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-gcp.txt
COPY skills/ /opt/skills/
RUN test -f /opt/skills/mongodb-log-diagnostic/SKILL.md && \
    head -n 1 /opt/skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py | grep -q '^#!/usr/bin/env python3'
COPY agent/ /app/agent/
WORKDIR /app
ENV SKILLS_DIR=/opt/skills DIAG_SKILL_DIR=/opt/skills/mongodb-log-diagnostic CLOUD_PROVIDER=gcp WORK_DIR=/tmp PYTHONUNBUFFERED=1
ENTRYPOINT ["python", "-m", "agent.handler"]
CMD ["--stage", "all"]
