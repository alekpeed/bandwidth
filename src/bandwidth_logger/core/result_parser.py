"""Turn raw engine output into normalised measurements.

Kept apart from the engine adapters so it can be exercised against recorded
fixture output with no subprocess involved.

The rule that shapes every function here: a value the engine did not supply
becomes ``None`` and is stored as NULL. A value the engine reported as zero
stays ``0``. Packet loss of 0% and packet loss that was never measured are
different facts and must never collapse into each other.
"""

from __future__ import annotations

import json
from typing import Any

from ..core.models import EngineMeasurement, ErrorCategory, bytes_per_second_to_bps


class ParseError(ValueError):
    """Engine output could not be read as a complete result."""

    category = ErrorCategory.MALFORMED_OUTPUT


# --------------------------------------------------------------------------
# Coercion helpers
# --------------------------------------------------------------------------


def dig(payload: Any, *keys: str) -> Any:
    """Follow a chain of mapping keys, returning ``None`` if any is absent."""
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def coerce_float(value: Any) -> float | None:
    """Float, or ``None`` for missing/unparseable input. Preserves ``0.0``."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None
    return None


def coerce_int(value: Any) -> int | None:
    """Integer, or ``None`` for missing/unparseable input. Preserves ``0``."""
    number = coerce_float(value)
    return None if number is None else int(round(number))


def coerce_str(value: Any) -> str | None:
    """Non-empty trimmed string, or ``None``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    return text or None


def load_json(stdout: str) -> dict[str, Any]:
    """Parse engine stdout as a JSON object.

    Engines occasionally emit progress lines before the result, so the last
    complete JSON object on its own line is used when the whole payload does
    not parse.
    """
    text = (stdout or "").strip()
    if not text:
        raise ParseError("The engine produced no output.")

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = _last_json_object(text)

    if not isinstance(payload, dict):
        raise ParseError("The engine did not return a JSON object.")
    return payload


def _last_json_object(text: str) -> Any:
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            continue
    raise ParseError("The engine output was not valid JSON.")


# --------------------------------------------------------------------------
# Ookla Speedtest CLI
# --------------------------------------------------------------------------


def parse_ookla_json(stdout: str, *, engine_version: str | None = None) -> EngineMeasurement:
    """Parse ``speedtest --format=json``.

    Ookla reports bandwidth in **bytes** per second; it is converted to bits
    per second here so both engines store the same unit.
    """
    payload = load_json(stdout)

    if payload.get("type") == "log" or "error" in payload:
        message = coerce_str(payload.get("error")) or "The engine reported an error."
        raise ParseError(message)

    download_bps = bytes_per_second_to_bps(coerce_float(dig(payload, "download", "bandwidth")))
    upload_bps = bytes_per_second_to_bps(coerce_float(dig(payload, "upload", "bandwidth")))

    if download_bps is None and upload_bps is None:
        raise ParseError("The engine result contained no download or upload measurement.")

    server_id = dig(payload, "server", "id")

    return EngineMeasurement(
        engine_name="ookla",
        engine_version=engine_version,
        download_bps=download_bps,
        upload_bps=upload_bps,
        idle_latency_ms=coerce_float(dig(payload, "ping", "latency")),
        download_latency_ms=coerce_float(dig(payload, "download", "latency", "iqm")),
        upload_latency_ms=coerce_float(dig(payload, "upload", "latency", "iqm")),
        jitter_ms=coerce_float(dig(payload, "ping", "jitter")),
        packet_loss_percent=coerce_float(payload.get("packetLoss")),
        server_id=coerce_str(server_id),
        server_name=coerce_str(dig(payload, "server", "name")),
        server_host=coerce_str(dig(payload, "server", "host")),
        server_city=coerce_str(dig(payload, "server", "location")),
        server_region=coerce_str(dig(payload, "server", "region")),
        server_country=coerce_str(dig(payload, "server", "country")),
        isp_name=coerce_str(payload.get("isp")),
        external_ip=coerce_str(dig(payload, "interface", "externalIp")),
        interface_name=coerce_str(dig(payload, "interface", "name")),
        raw_result_json=json.dumps(payload, sort_keys=True),
    )
