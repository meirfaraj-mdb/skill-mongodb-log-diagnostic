# Self-contained build: the diagnostic skill is vendored in skills/mongodb-log-diagnostic.
# Build from project root: docker build -f deploy/aws/Dockerfile -t mongodb-log-diagnostic-agent:aws .
FROM public.ecr.aws/lambda/python:3.12
COPY requirements-aws.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-aws.txt
COPY skills/ /opt/skills/
RUN test -f /opt/skills/mongodb-log-diagnostic/SKILL.md && \
    head -n 1 /opt/skills/mongodb-log-diagnostic/scripts/extract_mongodb_log.py | grep -q '^#!/usr/bin/env python3'
COPY agent/ ${LAMBDA_TASK_ROOT}/agent/
ENV SKILLS_DIR=/opt/skills DIAG_SKILL_DIR=/opt/skills/mongodb-log-diagnostic CLOUD_PROVIDER=aws WORK_DIR=/tmp
CMD ["agent.handler.lambda_handler"]
