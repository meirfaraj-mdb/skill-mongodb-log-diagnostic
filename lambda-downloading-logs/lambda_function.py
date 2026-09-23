import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta
from urllib.error import HTTPError, URLError
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


RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


class AtlasClient:
    def __init__(self, config, request_id="unknown"):
        self.base_url = config.get(
            "atlas_base_url",
            "https://cloud.mongodb.com/api/atlas/v2",
        )
        self.api_version = config["api_version"]
        self.timeout = int(config.get("http_timeout_seconds", 120))
        self.max_retries = int(config.get("max_retries", 5))
        self.request_id = request_id

        password_manager = HTTPPasswordMgrWithDefaultRealm()
        password_manager.add_password(
            None,
            "https://cloud.mongodb.com",
            config["atlas_public_key"],
            config["atlas_private_key"],
        )

        self.opener = build_opener(HTTPDigestAuthHandler(password_manager))

    def _log(self, level, message, *args):
        logger.log(level, "request_id=%s " + message, self.request_id, *args)

    @staticmethod
    def _retry_delay(attempt, retry_after=None):
        try:
            return max(0, int(retry_after))
        except (TypeError, ValueError):
            return min(2 ** (attempt - 1), 30)

    def _request(self, url, accept):
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            request = Request(
                url,
                headers={
                    "Accept": accept,
                    "User-Agent": "mongodb-atlas-log-archiver/1.0",
                },
                method="GET",
            )
            started = time.perf_counter()
            self._log(
                logging.INFO,
                "Atlas request started method=GET attempt=%d/%d url=%s accept=%s",
                attempt,
                self.max_retries,
                url,
                accept,
            )

            try:
                response = self.opener.open(request, timeout=self.timeout)
                elapsed = time.perf_counter() - started
                self._log(
                    logging.INFO,
                    "Atlas request succeeded attempt=%d status=%s elapsed_seconds=%.3f content_length=%s",
                    attempt,
                    getattr(response, "status", "unknown"),
                    elapsed,
                    response.headers.get("Content-Length"),
                )
                return response

            except HTTPError as error:
                elapsed = time.perf_counter() - started
                last_error = error
                retryable = error.code in RETRYABLE_HTTP_CODES
                retry_after = error.headers.get("Retry-After")

                self._log(
                    logging.WARNING if retryable else logging.ERROR,
                    "Atlas request returned HTTP error attempt=%d status=%s retryable=%s elapsed_seconds=%.3f retry_after=%s",
                    attempt,
                    error.code,
                    retryable,
                    elapsed,
                    retry_after,
                )

                if not retryable or attempt >= self.max_retries:
                    raise

                delay = self._retry_delay(attempt, retry_after)
                error.close()
                self._log(
                    logging.WARNING,
                    "Retrying Atlas request delay_seconds=%s next_attempt=%d",
                    delay,
                    attempt + 1,
                )
                time.sleep(delay)

            except URLError as error:
                elapsed = time.perf_counter() - started
                last_error = error
                self._log(
                    logging.WARNING,
                    "Atlas request network error attempt=%d elapsed_seconds=%.3f error=%s",
                    attempt,
                    elapsed,
                    error,
                )

                if attempt >= self.max_retries:
                    raise

                delay = self._retry_delay(attempt)
                self._log(
                    logging.WARNING,
                    "Retrying Atlas request after network error delay_seconds=%s next_attempt=%d",
                    delay,
                    attempt + 1,
                )
                time.sleep(delay)

            except Exception:
                elapsed = time.perf_counter() - started
                self._log(
                    logging.ERROR,
                    "Atlas request failed unexpectedly attempt=%d elapsed_seconds=%.3f",
                    attempt,
                    elapsed,
                )
                logger.exception("request_id=%s Atlas request exception details", self.request_id)
                raise

        # Defensive fallback; normally every path returns or raises above.
        raise last_error or RuntimeError("Atlas request failed without an error")

    def get_json(self, path, query=None):
        url = f"{self.base_url}{path}"
        if query:
            url += "?" + urlencode(query)

        accept = f"application/vnd.atlas.{self.api_version}+json"
        started = time.perf_counter()
        with self._request(url, accept) as response:
            payload = json.load(response)

        result_count = len(payload.get("results", [])) if isinstance(payload, dict) else None
        self._log(
            logging.INFO,
            "Atlas JSON response parsed path=%s result_count=%s elapsed_seconds=%.3f",
            path,
            result_count,
            time.perf_counter() - started,
        )
        return payload

    def download_log(self, group_id, host_name, log_name, start_date, end_date):
        path = (
            f"/groups/{quote(group_id, safe='')}"
            f"/clusters/{quote(host_name, safe='')}"
            f"/logs/{quote(log_name, safe='')}.gz"
        )
        query = urlencode({"startDate": start_date, "endDate": end_date})
        url = f"{self.base_url}{path}?{query}"
        accept = f"application/vnd.atlas.{self.api_version}+gzip"

        self._log(
            logging.INFO,
            "Atlas log download requested host=%s log=%s start_date=%s end_date=%s",
            host_name,
            log_name,
            start_date,
            end_date,
        )
        return self._request(url, accept)


def load_config(request_id="unknown"):
    secret_id = os.environ["ATLAS_SECRET_ID"]
    started = time.perf_counter()
    logger.info("request_id=%s Secrets Manager request started secret_id=%s", request_id, secret_id)

    try:
        value = secretsmanager.get_secret_value(SecretId=secret_id)
    except Exception:
        logger.exception("request_id=%s Secrets Manager request failed secret_id=%s", request_id, secret_id)
        raise

    logger.info(
        "request_id=%s Secrets Manager request succeeded secret_id=%s elapsed_seconds=%.3f",
        request_id,
        secret_id,
        time.perf_counter() - started,
    )

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

    logger.info(
        "request_id=%s Atlas configuration loaded group_id=%s cluster=%s bucket=%s",
        request_id,
        config["group_id"],
        config["cluster_name"],
        config["s3_bucket"],
    )
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

        logger.info(
            "request_id=%s Processes page received page=%d page_count=%d total_loaded=%d reported_total=%s",
            client.request_id,
            page_number,
            len(batch),
            len(processes),
            payload.get("totalCount"),
        )

        if not batch or len(batch) < page_size:
            break
        page_number += 1

    logger.info(
        "request_id=%s Process listing complete page_count=%d process_count=%d",
        client.request_id,
        page_number,
        len(processes),
    )
    return processes


def process_matches_cluster(process, config):
    hostname = process.get("hostname", "")
    user_alias = process.get("userAlias", "")

    explicit_hosts = config.get("hostnames")
    if explicit_hosts:
        return user_alias in set(explicit_hosts)

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
    request_id = getattr(context, "aws_request_id", "local")
    invocation_started = time.perf_counter()
    logger.info(
        "request_id=%s Lambda invocation started event_keys=%s",
        request_id,
        sorted(event.keys()) if isinstance(event, dict) else [],
    )

    try:
        config = load_config(request_id)
        client = AtlasClient(config, request_id)

        group_id = config["group_id"]
        cluster_name = config["cluster_name"]
        bucket = config["s3_bucket"]
        prefix = config.get("s3_prefix", "mongodb-atlas-logs").strip("/")
        start_date, end_date, log_date = previous_day_window(config["timezone"])

        logger.info(
            "request_id=%s Log window calculated start=%s end=%s log_date=%s timezone=%s",
            request_id,
            start_date,
            end_date,
            log_date,
            config["timezone"],
        )

        cluster = client.get_json(
            f"/groups/{quote(group_id, safe='')}"
            f"/clusters/{quote(cluster_name, safe='')}",
            {"pretty": "false"},
        )

        logger.info(
            "request_id=%s Cluster loaded name=%s cluster_type=%s date=%s",
            request_id,
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

        logger.info(
            "request_id=%s Process selection complete discovered=%d targets=%d",
            request_id,
            len(processes),
            len(target_processes),
        )

        if not target_processes:
            raise RuntimeError(
                "No hosts matched the cluster. Configure host_selector or "
                "hostnames in the secret if needed."
            )

        uploaded = []
        failed = []
        upload_attempts = 0

        for process in target_processes:
            host_name = process.get("hostname")
            if not host_name:
                logger.warning(
                    "request_id=%s Skipping process without hostname process_type=%s",
                    request_id,
                    process.get("typeName"),
                )
                continue

            log_names = log_names_for_process(process, config)
            logger.info(
                "request_id=%s Processing host host=%s process_type=%s logs=%s",
                request_id,
                host_name,
                process.get("typeName"),
                log_names,
            )

            for log_name in log_names:
                upload_attempts += 1
                key = (
                    f"{prefix}/{log_date}/"
                    f"{safe_filename(host_name)}/{log_name}.gz"
                )
                upload_started = time.perf_counter()
                response = None

                logger.info(
                    "request_id=%s Log archive started host=%s log=%s bucket=%s key=%s",
                    request_id,
                    host_name,
                    log_name,
                    bucket,
                    key,
                )

                try:
                    response = client.download_log(
                        group_id,
                        host_name,
                        log_name,
                        start_date,
                        end_date,
                    )
                    expected_bytes = response.headers.get("Content-Length")
                    logger.info(
                        "request_id=%s S3 upload started host=%s log=%s bucket=%s key=%s expected_bytes=%s",
                        request_id,
                        host_name,
                        log_name,
                        bucket,
                        key,
                        expected_bytes,
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

                    elapsed = time.perf_counter() - upload_started
                    uploaded_uri = f"s3://{bucket}/{key}"
                    uploaded.append(uploaded_uri)
                    logger.info(
                        "request_id=%s S3 upload succeeded host=%s log=%s bucket=%s key=%s expected_bytes=%s elapsed_seconds=%.3f",
                        request_id,
                        host_name,
                        log_name,
                        bucket,
                        key,
                        expected_bytes,
                        elapsed,
                    )

                except Exception as error:
                    logger.exception(
                        "request_id=%s Log archive failed host=%s log=%s bucket=%s key=%s elapsed_seconds=%.3f",
                        request_id,
                        host_name,
                        log_name,
                        bucket,
                        key,
                        time.perf_counter() - upload_started,
                    )
                    failed.append(
                        {
                            "host": host_name,
                            "log_name": log_name,
                            "error": str(error)[:500],
                        }
                    )
                finally:
                    if response is not None:
                        response.close()

        result = {
            "date": log_date,
            "cluster": cluster_name,
            "uploaded_count": len(uploaded),
            "failed_count": len(failed),
            "upload_attempts": upload_attempts,
            "uploaded": uploaded,
            "failed": failed,
        }

        logger.info(
            "request_id=%s Lambda processing complete upload_attempts=%d uploaded=%d failed=%d elapsed_seconds=%.3f",
            request_id,
            upload_attempts,
            len(uploaded),
            len(failed),
            time.perf_counter() - invocation_started,
        )

        if failed:
            logger.error("request_id=%s Lambda completed with failures result=%s", request_id, result)
            raise RuntimeError(json.dumps(result))

        return result

    except Exception:
        logger.exception(
            "request_id=%s Lambda invocation failed elapsed_seconds=%.3f",
            request_id,
            time.perf_counter() - invocation_started,
        )
        raise
