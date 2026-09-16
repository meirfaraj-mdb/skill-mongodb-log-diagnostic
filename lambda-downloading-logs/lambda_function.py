import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    Request,
    build_opener,
)
from zoneinfo import ZoneInfo

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

secretsmanager = boto3.client("secretsmanager")
s3 = boto3.client("s3")


class AtlasClient:
    def __init__(self, config):
        self.base_url = config.get(
            "atlas_base_url",
            "https://cloud.mongodb.com/api/atlas/v2",
        )
        self.api_version = config["api_version"]
        self.timeout = int(config.get("http_timeout_seconds", 120))
        self.max_retries = int(config.get("max_retries", 5))

        password_manager = HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            "https://cloud.mongodb.com",
            config["atlas_public_key"],
            config["atlas_private_key"],
        )

        self.opener = build_opener(
            HTTPDigestAuthHandler(password_manager)
        )

    def _request(self, url, accept):
        last_error = None

        for attempt in range(self.max_retries):
            request = Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": "mongodb-atlas-log-archiver/1.0",
                },
                method="GET",
            )

            try:
                return self.opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                last_error = error

                if error.code not in (429, 500, 502, 503, 504):
                    raise

                retry_after = error.headers.get("Retry-After")
                try:
                    delay = int(retry_after)
                except (TypeError, ValueError):
                    delay = min(2 ** attempt, 30)

                error.close()
                time.sleep(delay)

        raise last_error

    def get_json(self, path, query=None):
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlencode(query)

        accept = f"application/vnd.atlas.{self.api_version}+json"
        with self._request(url, accept) as response:
            return json.load(response)

    def download_log(self, group_id, host_name, log_name, start_date, end_date):
        path = (
            f"/groups/{quote(group_id, safe='')}"
            f"/clusters/{quote(host_name, safe='')}"
            f"/logs/{quote(log_name, safe='')}.gz"
        )
        query = urlencode({"startDate": start_date, "endDate": end_date})
        url = f"{self.base_url}{path}?{query}"
        accept = f"application/vnd.atlas.{self.api_version}+gzip"
        return self._request(url, accept)


def load_config():
    secret_id = os.environ["ATLAS_SECRET_ID"]
    value = secretsmanager.get_secret_value(SecretId=secret_id)

    if value.get("SecretString"):
        secret_string = value["SecretString"]
    else:
        secret_binary = value["SecretBinary"]
        if isinstance(secret_binary, str):
            secret_binary = base64.b64decode(secret_binary)
        secret_string = secret_binary.decode("utf-8")

    config = json.loads(secret_string)
    required = [
        "atlas_public_key",
        "atlas_private_key",
        "group_id",
        "cluster_name",
        "s3_bucket",
        "timezone",
        "api_version",
    ]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing secret values: {', '.join(missing)}")
    return config


def previous_day_window(timezone_name):
    local_zone = ZoneInfo(timezone_name)
    now = datetime.now(local_zone)
    end_local = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=1)
    return (
        int(start_local.timestamp()),
        int(end_local.timestamp()),
        start_local.date().isoformat(),
    )


def list_processes(client, group_id):
    processes = []
    page_number = 1
    page_size = 500

    while True:
        payload = client.get_json(
            f"/groups/{quote(group_id, safe='')}/processes",
            {
                "itemsPerPage": page_size,
                "pageNum": page_number,
                "includeCount": "true",
                "pretty": "false",
            },
        )
        batch = payload.get("results", [])
        processes.extend(batch)

        if not batch or len(batch) < page_size:
            break
        page_number += 1

    return processes


def process_matches_cluster(process, config):
    hostname = process.get("hostname", "")
    user_alias = process.get("userAlias", "")

    explicit_hosts = config.get("hostnames")
    if explicit_hosts:
        return hostname in set(explicit_hosts)

    host_selector = config.get("host_selector")
    if host_selector:
        return bool(re.search(host_selector, f"{hostname} {user_alias}", re.I))

    cluster_name = config["cluster_name"].lower()
    values = [str(hostname).lower(), str(user_alias).lower()]
    return any(
        value == cluster_name or value.startswith(f"{cluster_name}-")
        for value in values
    )


def log_names_for_process(process, config):
    configured_logs = config.get("log_names", ["auto"])
    if isinstance(configured_logs, str):
        configured_logs = [configured_logs]

    is_mongos = process.get("typeName") == "SHARD_MONGOS"
    if "auto" in configured_logs:
        return ["mongos" if is_mongos else "mongodb"]

    selected = []
    for log_name in configured_logs:
        if is_mongos and log_name.startswith("mongodb"):
            continue
        if not is_mongos and log_name.startswith("mongos"):
            continue
        selected.append(log_name)
    return selected


def safe_filename(value):
    return re.sub(r"[^A-Za-z0-9._-]", "_", value)


def lambda_handler(event, context):
    config = load_config()
    client = AtlasClient(config)

    group_id = config["group_id"]
    cluster_name = config["cluster_name"]
    bucket = config["s3_bucket"]
    prefix = config.get("s3_prefix", "mongodb-atlas-logs").strip("/")
    start_date, end_date, log_date = previous_day_window(config["timezone"])

    cluster = client.get_json(
        f"/groups/{quote(group_id, safe='')}"
        f"/clusters/{quote(cluster_name, safe='')}",
        {"pretty": "false"},
    )

    logger.info(
        "Processing cluster=%s cluster_type=%s date=%s",
        cluster.get("name", cluster_name),
        cluster.get("clusterType", "unknown"),
        log_date,
    )

    processes = list_processes(client, group_id)
    target_processes = [
        process
        for process in processes
        if process.get("typeName") != "NO_DATA"
        and process_matches_cluster(process, config)
    ]

    if not target_processes:
        raise RuntimeError(
            "No hosts matched the cluster. Configure host_selector or "
            "hostnames in the secret if needed."
        )

    uploaded = []
    failed = []

    for process in target_processes:
        host_name = process.get("hostname")
        if not host_name:
            continue

        for log_name in log_names_for_process(process, config):
            key = (
                f"{prefix}/{log_date}/"
                f"{safe_filename(host_name)}/{log_name}.gz"
            )

            try:
                response = client.download_log(
                    group_id,
                    host_name,
                    log_name,
                    start_date,
                    end_date,
                )
                with response:
                    s3.upload_fileobj(
                        response,
                        bucket,
                        key,
                        ExtraArgs={
                            "ContentType": "application/gzip",
                            "Metadata": {
                                "cluster": cluster_name,
                                "host": host_name,
                                "log-name": log_name,
                                "start-date": str(start_date),
                                "end-date": str(end_date),
                            },
                        },
                    )
                uploaded.append(f"s3://{bucket}/{key}")

            except Exception as error:
                logger.exception("Failed host=%s log=%s", host_name, log_name)
                failed.append({
                    "host": host_name,
                    "log_name": log_name,
                    "error": str(error)[:500],
                })

    result = {
        "date": log_date,
        "cluster": cluster_name,
        "uploaded_count": len(uploaded),
        "failed_count": len(failed),
        "uploaded": uploaded,
        "failed": failed,
    }

    if failed:
        raise RuntimeError(json.dumps(result))

    return result
