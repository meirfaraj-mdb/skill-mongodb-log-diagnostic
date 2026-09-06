#!/usr/bin/env python3
"""Decode MongoDB FTDC blocks into timestamped metric records.

The implementation follows the block layout used by simagix/mongo-ftdc:
framed BSON documents contain type 0 metadata or type 1 compressed metric
blocks; metric blocks contain an attribute document, attribute/delta counts,
and cumulative varint deltas with zero-run compression.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import math
import struct
import sys
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO


@dataclass(frozen=True)
class BsonDateTime:
    millis: int


@dataclass(frozen=True)
class BsonTimestamp:
    seconds: int
    increment: int


@dataclass(frozen=True)
class BsonObjectId:
    value: bytes


class BsonDocument(list[tuple[str, Any]]):
    """Ordered BSON document preserving duplicate keys like bson.D."""


@dataclass
class AttributeSeries:
    path: str
    values: list[int]
    date_value: bool = False


@dataclass
class MetricBlock:
    attributes: list[AttributeSeries]
    num_deltas: int



def _cstring(data: bytes, offset: int, end: int) -> tuple[str, int]:
    stop = data.find(b"\x00", offset, end)
    if stop < 0:
        raise ValueError("unterminated BSON key")
    return data[offset:stop].decode("utf-8", "replace"), stop + 1


def _read_document(data: bytes, start: int = 0) -> tuple[BsonDocument, int]:
    if start + 5 > len(data):
        raise ValueError("short BSON document")
    length = struct.unpack_from("<i", data, start)[0]
    end = start + length
    if length < 5 or end > len(data) or data[end - 1] != 0:
        raise ValueError("invalid BSON document length")
    pos = start + 4
    document = BsonDocument()
    while pos < end - 1:
        kind = data[pos]
        pos += 1
        key, pos = _cstring(data, pos, end)
        value, pos = _read_value(data, pos, end, kind)
        document.append((key, value))
    return document, end


def _read_string(data: bytes, offset: int) -> tuple[str, int]:
    size = struct.unpack_from("<i", data, offset)[0]
    if size < 1 or offset + 4 + size > len(data):
        raise ValueError("invalid BSON string")
    start = offset + 4
    return data[start:start + size - 1].decode("utf-8", "replace"), start + size


def _read_value(data: bytes, offset: int, end: int, kind: int) -> tuple[Any, int]:
    if kind == 0x01:
        return struct.unpack_from("<d", data, offset)[0], offset + 8
    if kind == 0x02:
        return _read_string(data, offset)
    if kind == 0x03:
        return _read_document(data, offset)
    if kind == 0x04:
        document, next_offset = _read_document(data, offset)
        ordered = sorted(document, key=lambda item: int(item[0]) if item[0].isdigit() else item[0])
        return [value for _, value in ordered], next_offset
    if kind == 0x05:
        size = struct.unpack_from("<i", data, offset)[0]
        if size < 0 or offset + 5 + size > len(data):
            raise ValueError("invalid BSON binary")
        subtype = data[offset + 4]
        payload_start = offset + 5
        payload = data[payload_start:payload_start + size]
        return payload, payload_start + size
    if kind == 0x07:
        return BsonObjectId(data[offset:offset + 12]), offset + 12
    if kind == 0x08:
        return bool(data[offset]), offset + 1
    if kind == 0x09:
        return BsonDateTime(struct.unpack_from("<q", data, offset)[0]), offset + 8
    if kind == 0x0A:
        return None, offset
    if kind == 0x0B:
        _, offset = _cstring(data, offset, end)
        _, offset = _cstring(data, offset, end)
        return None, offset
    if kind == 0x0D or kind == 0x0E:
        return _read_string(data, offset)
    if kind == 0x10:
        return struct.unpack_from("<i", data, offset)[0], offset + 4
    if kind == 0x11:
        raw = struct.unpack_from("<Q", data, offset)[0]
        return BsonTimestamp(raw >> 32, raw & 0xFFFFFFFF), offset + 8
    if kind == 0x12:
        return struct.unpack_from("<q", data, offset)[0], offset + 8
    raise ValueError(f"unsupported BSON type 0x{kind:02x}")


def _mapping(document: BsonDocument) -> dict[str, Any]:
    return {key: value for key, value in document}


def _join(parent: str, child: str) -> str:
    return f"{parent}/{child}" if parent else f"/{child}"


def _append_attribute(attributes: list[AttributeSeries], path: str, value: int, date_value: bool = False) -> None:
    attributes.append(AttributeSeries(path=path, values=[max(0, int(value))], date_value=date_value))


def _flatten(value: Any, parent: str, attributes: list[AttributeSeries]) -> None:
    if isinstance(value, BsonDocument):
        for key, child in value:
            _flatten(child, _join(parent, key), attributes)
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _flatten(child, _join(parent, str(index)), attributes)
        return
    if isinstance(value, BsonTimestamp):
        _append_attribute(attributes, _join(parent, "t"), value.seconds)
        _append_attribute(attributes, _join(parent, "i"), value.increment)
        return
    if isinstance(value, BsonDateTime):
        _append_attribute(attributes, parent, value.millis, date_value=True)
        return
    if isinstance(value, bool):
        _append_attribute(attributes, parent, int(value))
        return
    if isinstance(value, int):
        _append_attribute(attributes, parent, value)
        return
    if isinstance(value, float) and math.isfinite(value):
        _append_attribute(attributes, parent, int(value))


def _uvarint(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    for index in range(10):
        if offset >= len(data):
            raise ValueError("truncated FTDC varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if byte < 0x80:
            if index == 9 and byte > 1:
                raise ValueError("FTDC varint overflow")
            return value, offset
        shift += 7
    raise ValueError("FTDC varint overflow")


def _decode_metric_block(buffer: bytes) -> MetricBlock:
    first_document, offset = _read_document(buffer)
    if offset + 8 > len(buffer):
        raise ValueError("short FTDC metric block header")
    num_attributes, num_deltas = struct.unpack_from("<II", buffer, offset)
    offset += 8
    attributes: list[AttributeSeries] = []
    _flatten(first_document, "", attributes)
    if len(attributes) != num_attributes:
        raise ValueError(f"inconsistent FTDC attributes: expected {num_attributes}, got {len(attributes)}")

    zeros_left = 0
    for attribute in attributes:
        for _ in range(num_deltas):
            if zeros_left:
                delta = 0
                zeros_left -= 1
            else:
                delta, offset = _uvarint(buffer, offset)
                if delta == 0:
                    zeros_left, offset = _uvarint(buffer, offset)
            attribute.values.append(attribute.values[-1] + delta)
    return MetricBlock(attributes=attributes, num_deltas=num_deltas)


def _millis_to_iso(millis: int) -> str | None:
    try:
        return dt.datetime.fromtimestamp(millis / 1000, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _rows(block: MetricBlock) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index in range(block.num_deltas + 1):
        row: dict[str, Any] = {}
        for attribute in block.attributes:
            key = attribute.path.lstrip("/") or "value"
            value = attribute.values[index]
            row[key] = value
            terminal = attribute.path.rsplit("/", 1)[-1].lower()
            if attribute.date_value or terminal in {"timestamp", "localtime", "wall", "date", "time"}:
                timestamp = _millis_to_iso(value)
                if timestamp:
                    row["timestamp"] = timestamp
        rows.append(row)
    return rows


def _json_records(data: bytes) -> tuple[list[dict[str, Any]], bool]:
    text = data.decode("utf-8", "replace").strip()
    if not text or text[0] not in "[{":
        return [], False
    try:
        decoded = json.loads(text)
        if isinstance(decoded, dict):
            return [decoded], True
        if isinstance(decoded, list):
            return [item for item in decoded if isinstance(item, dict)], True
    except json.JSONDecodeError:
        pass
    records = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records, bool(records)


def _decode_bson_stream(data: bytes) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
    records: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    errors: list[str] = []
    offset = 0
    while offset < len(data):
        try:
            document, end = _read_document(data, offset)
        except (IndexError, struct.error, ValueError) as exc:
            if not records:
                raise ValueError(f"unable to decode FTDC BSON stream: {exc}") from exc
            errors.append(str(exc))
            break
        offset = end
        mapping = _mapping(document)
        record_type = mapping.get("type")
        if record_type == 0:
            source_doc = mapping.get("doc")
            if isinstance(source_doc, (BsonDocument, dict)):
                metadata = _jsonable(source_doc)
        elif record_type == 1:
            binary = mapping.get("data")
            if not isinstance(binary, (bytes, bytearray)) or len(binary) < 4:
                errors.append("type-1 FTDC block has no compressed data")
                continue
            try:
                decompressed = zlib.decompress(binary[4:])
                records.extend(_rows(_decode_metric_block(decompressed)))
            except (ValueError, zlib.error, struct.error) as exc:
                errors.append(str(exc))
        elif mapping:
            records.append(_jsonable(mapping))
    return records, metadata, errors


def _jsonable(value: Any) -> Any:
    if isinstance(value, BsonDocument):
        return {key: _jsonable(child) for key, child in value}
    if isinstance(value, list):
        return [_jsonable(child) for child in value]
    if isinstance(value, BsonDateTime):
        return _millis_to_iso(value.millis) or value.millis
    if isinstance(value, BsonTimestamp):
        return {"t": value.seconds, "i": value.increment}
    if isinstance(value, BsonObjectId):
        return value.value.hex()
    if isinstance(value, bytes):
        return value.hex()
    return value


def decode_ftdc_bytes(data: bytes) -> dict[str, Any]:
    """Decode JSON/JSONL or MongoDB's framed BSON FTDC stream."""
    records, is_json = _json_records(data)
    if is_json:
        return {"records": records, "metadata": {}, "errors": [], "source_kind": "json"}
    records, metadata, errors = _decode_bson_stream(data)
    return {"records": records, "metadata": metadata, "errors": errors, "source_kind": "bson"}


def decode_ftdc_file(path: Path) -> dict[str, Any]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    with opener(path, "rb") as handle:
        data = handle.read()
    result = decode_ftdc_bytes(data)
    result["path"] = str(path)
    result["data"] = data
    return result


def emit_source_debug(path: Path, result: dict[str, Any], stream: TextIO = sys.stderr) -> None:
    """Print source input/decoded records, never the filtered extraction output."""
    print(f"FTDC SOURCE JSON: {path}", file=stream)
    data = result.get("data", b"")
    if result.get("source_kind") == "json":
        print(data.decode("utf-8", "replace").strip(), file=stream)
        return
    for record in result.get("records", []):
        print(json.dumps(record, sort_keys=True, default=str), file=stream)
    for error in result.get("errors", []):
        print(f"FTDC SOURCE DECODER ERROR: {error}", file=stream)
