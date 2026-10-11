"""Read-only inspection of the running Agent Engine container's CA trust.

This inspects filesystem trust locations, not the Docker image build history.
No certificates, environment secrets, or full CA inventories are logged.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
from pathlib import Path

logger = logging.getLogger(__name__)
FINGERPRINT_ENV = "MONGODB_LOG_DIAG_GATEWAY_CA_SHA256"
_OVERRIDE_NAMES = (
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH",
)
_PEM = re.compile(rb"-----BEGIN CERTIFICATE-----\s*.*?\s*-----END CERTIFICATE-----", re.S)
_MAX_FILE_BYTES = 4 * 1024 * 1024
_MAX_DIRECTORY_FILES = 4096
_DIRECTORIES = (
    "/etc/ssl/certs", "/usr/local/share/ca-certificates",
    "/usr/share/ca-certificates", "/etc/pki/ca-trust/extracted/pem",
)
_FILES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
          "/etc/pki/tls/certs/ca-bundle.crt")


def _normalize_fingerprint(value: str) -> str | None:
    normalized = value.replace(":", "").strip().upper()
    if not normalized:
        return None
    if not re.fullmatch(r"[0-9A-F]{64}", normalized):
        raise ValueError(f"{FINGERPRINT_ENV} must be a 64-digit SHA-256 hex fingerprint")
    return normalized


def _file_has_fingerprint(path: Path, fingerprint: str) -> tuple[bool, str | None]:
    """Inspect a bounded CA file; never return or log certificate bytes."""
    try:
        if not path.is_file():
            return False, "not_a_file"
        size = path.stat().st_size
        if size > _MAX_FILE_BYTES:
            return False, "too_large"
        data = path.read_bytes()
        if b"-----BEGIN CERTIFICATE-----" in data:
            for block in _PEM.findall(data):
                try:
                    der = ssl.PEM_cert_to_DER_cert(block.decode("ascii"))
                except (ValueError, UnicodeDecodeError, ssl.SSLError):
                    continue
                if hashlib.sha256(der).hexdigest().upper() == fingerprint:
                    return True, None
        elif data.startswith(b"\x30") and hashlib.sha256(data).hexdigest().upper() == fingerprint:
            return True, None
        return False, None
    except (OSError, ValueError) as exc:
        return False, type(exc).__name__


def _scan_store(files: list[str], directories: list[str], fingerprint: str) -> dict:
    """Inspect only standard CA locations with strict file/count limits."""
    info = {"candidate_files_checked": 0, "candidate_directories_checked": 0,
            "matching_paths": [], "unreadable_or_skipped": 0, "scan_truncated": False}
    seen: set[str] = set()
    budget = _MAX_DIRECTORY_FILES

    def check(path: Path) -> None:
        nonlocal budget
        if budget <= 0:
            info["scan_truncated"] = True
            return
        try:
            canonical = str(path.resolve(strict=False))
        except (OSError, RuntimeError):
            info["unreadable_or_skipped"] += 1
            return
        if canonical in seen:
            return
        seen.add(canonical)
        budget -= 1
        info["candidate_files_checked"] += 1
        found, error = _file_has_fingerprint(path, fingerprint)
        if found and len(info["matching_paths"]) < 8:
            info["matching_paths"].append(str(path))
        if error:
            info["unreadable_or_skipped"] += 1

    for name in files:
        if name and Path(name).exists():
            check(Path(name))
    for name in directories:
        directory = Path(name)
        if not directory.is_dir():
            continue
        info["candidate_directories_checked"] += 1
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if budget <= 0:
                        info["scan_truncated"] = True
                        break
                    # CA directories normally contain .pem/.crt files and OpenSSL hash links.
                    if entry.name.endswith((".pem", ".crt", ".cer")) or re.search(r"\.[0-9]+$", entry.name):
                        check(Path(entry.path))
        except OSError:
            info["unreadable_or_skipped"] += 1
    info["gateway_ca_found_in_scanned_files"] = bool(info["matching_paths"])
    return info


def gateway_tls_trust_diagnostic() -> dict:
    """Inspect system paths and Python trust; no network calls or TLS changes."""
    paths = ssl.get_default_verify_paths()
    result = {
        "diagnostic_module_path": __file__,
        "default_verify_paths": {
            "cafile": paths.cafile, "capath": paths.capath,
            "openssl_cafile": paths.openssl_cafile, "openssl_capath": paths.openssl_capath,
        },
        "ca_override_paths": {key: os.environ[key] for key in _OVERRIDE_NAMES if key in os.environ},
        "gateway_ca_sha256": os.environ.get(FINGERPRINT_ENV, "").replace(":", "").strip().upper() or None,
    }
    try:
        fingerprint = _normalize_fingerprint(os.environ.get(FINGERPRINT_ENV, ""))
    except ValueError as exc:
        result["diagnostic_error"] = str(exc)
        return result
    if not fingerprint:
        result["fingerprint_check_status"] = f"not_checked_set_{FINGERPRINT_ENV}"
        return result
    try:
        certs = ssl.create_default_context().get_ca_certs(binary_form=True)
        result["python_default_context_ca_count"] = len(certs)
        result["gateway_ca_in_python_default_context"] = any(
            hashlib.sha256(cert).hexdigest().upper() == fingerprint for cert in certs
        )
    except (OSError, ssl.SSLError) as exc:
        result["python_context_error"] = type(exc).__name__
    files = list(_FILES)
    for name in (paths.cafile, paths.openssl_cafile):
        if name:
            files.append(name)
    try:
        import certifi
        result["certifi_bundle_path"] = certifi.where()
        files.append(certifi.where())
    except ImportError:
        result["certifi_bundle_path"] = None
    directories = list(_DIRECTORIES)
    for name in (paths.capath, paths.openssl_capath, os.environ.get("SSL_CERT_DIR")):
        if name:
            directories.append(name)
    for key in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "GRPC_DEFAULT_SSL_ROOTS_FILE_PATH"):
        if os.environ.get(key):
            files.append(os.environ[key])
    result["container_ca_file_scan"] = _scan_store(files, directories, fingerprint)
    result["grpc_effective_roots_checked"] = False  # gRPC can use its own root bundle.
    return result


def log_gateway_tls_trust() -> None:
    """Log one bounded, non-secret diagnostic on import; never block startup."""
    try:
        logger.info("Gateway TLS trust diagnostic (no certificate contents): %s",
                    gateway_tls_trust_diagnostic())
    except Exception:
        logger.exception("Could not collect Gateway TLS trust diagnostic")
