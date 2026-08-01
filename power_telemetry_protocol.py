"""Shared validation and UDP transport for power telemetry records.

The monitor writes every record to local durable storage before publishing it.
UDP is intentionally used only as a best-effort live telemetry channel; the
Jetson-side JSONL log remains the authoritative local record.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from typing import Any, Mapping

SCHEMA_VERSION = 1
DEFAULT_POWER_TELEMETRY_PORT = 6001
MAX_POWER_TELEMETRY_DATAGRAM_BYTES = 60_000

_ALLOWED_RECORD_TYPES = {
    "monitor_startup",
    "monitor_shutdown",
    "power_sample",
    "power_state_event",
    "sensor_health_event",
    "telemetry_health_event",
}

_ALLOWED_STATES = {
    "STARTING",
    "LOW",
    "NORMAL",
    "OUT_OF_RANGE",
    "TRIP_PENDING",
    "SENSOR_DEGRADED",
    "SENSOR_ERROR",
    "STOPPED",
}


class PowerTelemetryProtocolError(ValueError):
    """Raised when a telemetry record or datagram violates the protocol."""


def _require_non_empty_string(record: Mapping[str, Any], field: str) -> str:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        raise PowerTelemetryProtocolError(
            f"{field} must be a non-empty string"
        )
    return value


def _require_integer(
    record: Mapping[str, Any],
    field: str,
    *,
    minimum: int = 0,
) -> int:
    value = record.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise PowerTelemetryProtocolError(f"{field} must be an integer")
    if value < minimum:
        raise PowerTelemetryProtocolError(
            f"{field} must be at least {minimum}"
        )
    return value


def validate_power_telemetry_record(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and return a shallow copy of one telemetry record."""
    if not isinstance(value, Mapping):
        raise PowerTelemetryProtocolError(
            "Power telemetry record must be a JSON object"
        )

    record = dict(value)

    schema_version = _require_integer(
        record,
        "schema_version",
        minimum=1,
    )
    if schema_version != SCHEMA_VERSION:
        raise PowerTelemetryProtocolError(
            f"Unsupported schema_version={schema_version}; "
            f"expected {SCHEMA_VERSION}"
        )

    record_type = _require_non_empty_string(record, "record_type")
    if record_type not in _ALLOWED_RECORD_TYPES:
        raise PowerTelemetryProtocolError(
            f"Unsupported record_type={record_type!r}"
        )

    _require_integer(record, "sequence", minimum=1)
    _require_integer(record, "monotonic_ns", minimum=0)
    _require_non_empty_string(record, "recorded_at_utc")
    _require_non_empty_string(record, "monitor_session_id")
    _require_non_empty_string(record, "run_id")
    _require_non_empty_string(record, "jetson_id")
    _require_non_empty_string(record, "boot_id")
    _require_non_empty_string(record, "configuration_fingerprint")
    _require_non_empty_string(record, "software_version")
    _require_non_empty_string(record, "git_commit")

    state = _require_non_empty_string(record, "measurement_state")
    if state not in _ALLOWED_STATES:
        raise PowerTelemetryProtocolError(
            f"Unsupported measurement_state={state!r}"
        )

    latched = record.get("red_flag_latched")
    if not isinstance(latched, bool):
        raise PowerTelemetryProtocolError(
            "red_flag_latched must be a Boolean"
        )

    event_type = record.get("event_type")
    if event_type is not None and (
        not isinstance(event_type, str) or not event_type.strip()
    ):
        raise PowerTelemetryProtocolError(
            "event_type must be null or a non-empty string"
        )

    if record_type == "power_sample":
        current_ma = _require_integer(record, "current_ma", minimum=0)
        if current_ma > 1_000_000:
            raise PowerTelemetryProtocolError(
                "current_ma exceeds protocol plausibility limit"
            )

    for field in ("voltage_mv", "power_mw", "current_ma"):
        if field in record and record[field] is not None:
            _require_integer(record, field, minimum=0)

    return record


def encode_power_telemetry_record(
    record: Mapping[str, Any],
    *,
    max_bytes: int = MAX_POWER_TELEMETRY_DATAGRAM_BYTES,
) -> bytes:
    """Validate and encode one compact UTF-8 JSON datagram."""
    validated = validate_power_telemetry_record(record)
    payload = json.dumps(
        validated,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")

    if len(payload) > max_bytes:
        raise PowerTelemetryProtocolError(
            f"Telemetry datagram is {len(payload)} bytes; "
            f"maximum is {max_bytes}"
        )
    return payload


def decode_power_telemetry_datagram(
    payload: bytes,
    *,
    max_bytes: int = MAX_POWER_TELEMETRY_DATAGRAM_BYTES,
) -> dict[str, Any]:
    """Decode and validate one UTF-8 JSON telemetry datagram."""
    if not isinstance(payload, bytes):
        raise TypeError("payload must be bytes")
    if not payload:
        raise PowerTelemetryProtocolError("Telemetry datagram is empty")
    if len(payload) > max_bytes:
        raise PowerTelemetryProtocolError(
            f"Telemetry datagram exceeds {max_bytes} bytes"
        )

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PowerTelemetryProtocolError(
            "Telemetry datagram is not valid UTF-8"
        ) from error

    try:
        value = json.loads(text)
    except json.JSONDecodeError as error:
        raise PowerTelemetryProtocolError(
            "Telemetry datagram is not valid JSON"
        ) from error

    if not isinstance(value, dict):
        raise PowerTelemetryProtocolError(
            "Telemetry datagram root must be a JSON object"
        )
    return validate_power_telemetry_record(value)


class UdpTelemetryPublisher:
    """Best-effort publisher for one JSON record per UDP datagram."""

    def __init__(
        self,
        host: str,
        port: int = DEFAULT_POWER_TELEMETRY_PORT,
        *,
        max_datagram_bytes: int = MAX_POWER_TELEMETRY_DATAGRAM_BYTES,
        udp_socket: socket.socket | None = None,
    ) -> None:
        if not isinstance(host, str) or not host.strip():
            raise ValueError("host must be a non-empty string")
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError("port must be an integer")
        if not 1 <= port <= 65_535:
            raise ValueError("port must be from 1 to 65535")
        if max_datagram_bytes <= 0:
            raise ValueError("max_datagram_bytes must be positive")

        self.host = host.strip()
        self.port = port
        self.max_datagram_bytes = max_datagram_bytes
        self._socket = udp_socket or socket.socket(
            socket.AF_INET,
            socket.SOCK_DGRAM,
        )
        self._owns_socket = udp_socket is None
        self.publish_count = 0
        self.failure_count = 0
        self.last_error: str | None = None

    def publish(self, record: Mapping[str, Any]) -> bool:
        """Publish a record; return False instead of crashing on socket error."""
        payload = encode_power_telemetry_record(
            record,
            max_bytes=self.max_datagram_bytes,
        )
        try:
            sent = self._socket.sendto(
                payload,
                (self.host, self.port),
            )
            if sent != len(payload):
                raise OSError(
                    f"UDP send wrote {sent} of {len(payload)} bytes"
                )
        except OSError as error:
            self.failure_count += 1
            self.last_error = str(error)
            return False

        self.publish_count += 1
        self.last_error = None
        return True

    def close(self) -> None:
        if self._owns_socket:
            self._socket.close()

    def __enter__(self) -> "UdpTelemetryPublisher":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class PowerTelemetrySnapshot:
    """Latest operator-facing view derived from accepted records."""

    monitor_session_id: str
    run_id: str
    jetson_id: str
    boot_id: str
    sequence: int
    recorded_at_utc: str
    received_monotonic: float
    measurement_state: str
    red_flag_latched: bool
    current_ma: int | None
    voltage_mv: int | None
    power_mw: int | None
    event_type: str | None


class PowerTelemetryModel:
    """Reject duplicate/out-of-order records and maintain the latest snapshot."""

    def __init__(self) -> None:
        self._last_sequence_by_session: dict[str, int] = {}
        self._active_session_id: str | None = None
        self._retired_session_ids: set[str] = set()
        self._latest: PowerTelemetrySnapshot | None = None

    @property
    def latest(self) -> PowerTelemetrySnapshot | None:
        return self._latest

    def apply(
        self,
        record: Mapping[str, Any],
        *,
        received_monotonic: float,
    ) -> PowerTelemetrySnapshot | None:
        validated = validate_power_telemetry_record(record)
        session_id = validated["monitor_session_id"]
        sequence = validated["sequence"]

        if session_id in self._retired_session_ids:
            return None
        if self._active_session_id is None:
            self._active_session_id = session_id
        elif session_id != self._active_session_id:
            self._retired_session_ids.add(self._active_session_id)
            self._active_session_id = session_id

        previous_sequence = self._last_sequence_by_session.get(session_id, 0)
        if sequence <= previous_sequence:
            return None
        self._last_sequence_by_session[session_id] = sequence

        prior = self._latest
        same_session = (
            prior is not None
            and prior.monitor_session_id == session_id
        )

        current_ma = validated.get("current_ma")
        voltage_mv = validated.get("voltage_mv")
        power_mw = validated.get("power_mw")

        if same_session and prior is not None:
            if current_ma is None:
                current_ma = prior.current_ma
            if voltage_mv is None:
                voltage_mv = prior.voltage_mv
            if power_mw is None:
                power_mw = prior.power_mw

        snapshot = PowerTelemetrySnapshot(
            monitor_session_id=session_id,
            run_id=validated["run_id"],
            jetson_id=validated["jetson_id"],
            boot_id=validated["boot_id"],
            sequence=sequence,
            recorded_at_utc=validated["recorded_at_utc"],
            received_monotonic=float(received_monotonic),
            measurement_state=validated["measurement_state"],
            red_flag_latched=validated["red_flag_latched"],
            current_ma=current_ma,
            voltage_mv=voltage_mv,
            power_mw=power_mw,
            event_type=validated.get("event_type"),
        )
        self._latest = snapshot
        return snapshot
