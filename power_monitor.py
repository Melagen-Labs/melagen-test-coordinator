#!/usr/bin/env python3
"""Read-only Jetson VDD_IN monitor with durable JSONL telemetry.

Safety boundary
---------------
This program is diagnostic software. It reads the Jetson INA3221 hardware
monitor, classifies measurements, records events, and can publish telemetry.
It does not stop CUDA, shut down Linux, change INA3221 limits, operate a relay,
or remove electrical power.

The default thresholds are temporary development values. They are not approved
Single-Event Latchup protection limits and must be replaced after hardware
baseline measurements and electrical/radiation-test review.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 1
SOFTWARE_VERSION = "0.2.0"

MEASUREMENT_STARTING = "STARTING"
MEASUREMENT_LOW = "LOW"
MEASUREMENT_NORMAL = "NORMAL"
MEASUREMENT_OUT_OF_RANGE = "OUT_OF_RANGE"
MEASUREMENT_TRIP_PENDING = "TRIP_PENDING"
MEASUREMENT_SENSOR_DEGRADED = "SENSOR_DEGRADED"
MEASUREMENT_SENSOR_ERROR = "SENSOR_ERROR"
MEASUREMENT_STOPPED = "STOPPED"

RESPONSE_MODE_LOG_ONLY = "log_only"


class ConfigurationError(ValueError):
    """Raised when a monitor configuration is invalid."""


class SensorDataError(ValueError):
    """Raised when a sensor value is malformed, non-finite, or implausible."""


def utc_timestamp() -> str:
    """Return a Coordinated Universal Time timestamp in ISO 8601 form."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def read_boot_id() -> str:
    """Return the Linux boot identifier when available."""
    path = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value or "unknown"


def detect_git_commit() -> str:
    """Return the current Git commit without failing monitor startup."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return result.stdout.strip() or "unknown"


def stable_fingerprint(value: object) -> str:
    """Return a stable SHA-256 fingerprint for JSON-serializable content."""
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    return value


def _require_float(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigurationError(f"{name} must be finite")
    return result


@dataclass(frozen=True)
class MonitorConfig:
    """Validated development configuration using integer engineering units."""

    schema_version: int = SCHEMA_VERSION
    normal_min_current_ma: int = 1900
    normal_max_current_ma: int = 2100
    out_of_range_clear_current_ma: int = 2050
    critical_current_ma: int = 2300
    critical_clear_current_ma: int = 2250
    critical_duration_seconds: float = 3.0
    critical_clear_duration_seconds: float = 0.5
    critical_clear_consecutive_samples: int = 3
    out_of_range_clear_consecutive_samples: int = 3
    sample_interval_seconds: float = 0.2
    sync_interval_seconds: float = 1.0
    sensor_error_consecutive_failures: int = 3
    sensor_error_timeout_seconds: float = 1.0
    min_plausible_current_ma: int = 0
    max_plausible_current_ma: int = 10000
    min_plausible_voltage_mv: int = 1000
    max_plausible_voltage_mv: int = 30000
    max_plausible_power_mw: int = 200000
    latch_red_flag: bool = True
    prohibit_reset_during_active_run: bool = True
    response_mode: str = RESPONSE_MODE_LOG_ONLY

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> "MonitorConfig":
        """Create a configuration from a strict JSON object."""
        allowed = set(cls.__dataclass_fields__)
        unknown = sorted(set(raw) - allowed)
        if unknown:
            raise ConfigurationError(
                "Unknown configuration fields: " + ", ".join(unknown)
            )

        integer_fields = {
            "schema_version",
            "normal_min_current_ma",
            "normal_max_current_ma",
            "out_of_range_clear_current_ma",
            "critical_current_ma",
            "critical_clear_current_ma",
            "critical_clear_consecutive_samples",
            "out_of_range_clear_consecutive_samples",
            "sensor_error_consecutive_failures",
            "min_plausible_current_ma",
            "max_plausible_current_ma",
            "min_plausible_voltage_mv",
            "max_plausible_voltage_mv",
            "max_plausible_power_mw",
        }
        float_fields = {
            "critical_duration_seconds",
            "critical_clear_duration_seconds",
            "sample_interval_seconds",
            "sync_interval_seconds",
            "sensor_error_timeout_seconds",
        }
        bool_fields = {
            "latch_red_flag",
            "prohibit_reset_during_active_run",
        }

        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            if key in integer_fields:
                normalized[key] = _require_int(key, value)
            elif key in float_fields:
                normalized[key] = _require_float(key, value)
            elif key in bool_fields:
                if not isinstance(value, bool):
                    raise ConfigurationError(f"{key} must be a Boolean")
                normalized[key] = value
            elif key == "response_mode":
                if not isinstance(value, str):
                    raise ConfigurationError("response_mode must be a string")
                normalized[key] = value
            else:
                normalized[key] = value

        config = cls(**normalized)
        config.validate()
        return config

    @classmethod
    def from_json_file(cls, path: Path) -> "MonitorConfig":
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except OSError as error:
            raise ConfigurationError(
                f"Could not read configuration {path}: {error}"
            ) from error
        except json.JSONDecodeError as error:
            raise ConfigurationError(
                f"Invalid JSON in configuration {path}: {error}"
            ) from error

        if not isinstance(raw, dict):
            raise ConfigurationError("Configuration root must be a JSON object")
        return cls.from_mapping(raw)

    def validate(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ConfigurationError(
                f"schema_version must be {SCHEMA_VERSION}"
            )
        if self.normal_min_current_ma < 0:
            raise ConfigurationError(
                "normal_min_current_ma cannot be negative"
            )
        if self.normal_max_current_ma < self.normal_min_current_ma:
            raise ConfigurationError(
                "normal_max_current_ma must be >= normal_min_current_ma"
            )
        if not (
            self.normal_min_current_ma
            <= self.out_of_range_clear_current_ma
            <= self.normal_max_current_ma
        ):
            raise ConfigurationError(
                "out_of_range_clear_current_ma must be inside the normal band"
            )
        if self.critical_current_ma <= self.normal_max_current_ma:
            raise ConfigurationError(
                "critical_current_ma must exceed normal_max_current_ma"
            )
        if not (
            self.normal_max_current_ma
            < self.critical_clear_current_ma
            < self.critical_current_ma
        ):
            raise ConfigurationError(
                "critical_clear_current_ma must be above the normal band "
                "and below critical_current_ma"
            )
        if self.critical_duration_seconds <= 0:
            raise ConfigurationError(
                "critical_duration_seconds must be positive"
            )
        if self.critical_clear_duration_seconds <= 0:
            raise ConfigurationError(
                "critical_clear_duration_seconds must be positive"
            )
        if self.critical_clear_consecutive_samples <= 0:
            raise ConfigurationError(
                "critical_clear_consecutive_samples must be positive"
            )
        if self.out_of_range_clear_consecutive_samples <= 0:
            raise ConfigurationError(
                "out_of_range_clear_consecutive_samples must be positive"
            )
        if self.sample_interval_seconds <= 0:
            raise ConfigurationError(
                "sample_interval_seconds must be positive"
            )
        if self.sync_interval_seconds <= 0:
            raise ConfigurationError(
                "sync_interval_seconds must be positive"
            )
        if self.sensor_error_consecutive_failures <= 0:
            raise ConfigurationError(
                "sensor_error_consecutive_failures must be positive"
            )
        if self.sensor_error_timeout_seconds <= 0:
            raise ConfigurationError(
                "sensor_error_timeout_seconds must be positive"
            )
        if self.min_plausible_current_ma < 0:
            raise ConfigurationError(
                "min_plausible_current_ma cannot be negative"
            )
        if self.max_plausible_current_ma <= self.min_plausible_current_ma:
            raise ConfigurationError(
                "max_plausible_current_ma must exceed min_plausible_current_ma"
            )
        if self.min_plausible_voltage_mv <= 0:
            raise ConfigurationError(
                "min_plausible_voltage_mv must be positive"
            )
        if self.max_plausible_voltage_mv <= self.min_plausible_voltage_mv:
            raise ConfigurationError(
                "max_plausible_voltage_mv must exceed min_plausible_voltage_mv"
            )
        if self.max_plausible_power_mw <= 0:
            raise ConfigurationError(
                "max_plausible_power_mw must be positive"
            )
        if not self.latch_red_flag:
            raise ConfigurationError(
                "latch_red_flag must remain true in this stage"
            )
        if self.response_mode != RESPONSE_MODE_LOG_ONLY:
            raise ConfigurationError(
                "Only response_mode='log_only' is permitted in this stage"
            )

    def fingerprint(self) -> str:
        return stable_fingerprint(asdict(self))


@dataclass(frozen=True)
class SensorReading:
    """One coherent power-sensor observation in integer engineering units."""

    current_ma: int
    voltage_mv: int | None = None
    power_mw: int | None = None
    sensor_source: str = "unknown"
    data_quality_flags: tuple[str, ...] = ()

    @property
    def current_a(self) -> float:
        return self.current_ma / 1000.0

    @property
    def voltage_v(self) -> float | None:
        if self.voltage_mv is None:
            return None
        return self.voltage_mv / 1000.0

    @property
    def power_w(self) -> float | None:
        if self.power_mw is None:
            return None
        return self.power_mw / 1000.0


class CurrentSource(Protocol):
    def read(self) -> SensorReading:
        """Return one coherent current reading or raise a sensor exception."""


@dataclass(frozen=True)
class DetectorEvent:
    event_type: str
    details: dict[str, object]


@dataclass(frozen=True)
class DetectorResult:
    measurement_state: str
    red_flag_latched: bool
    above_critical_seconds: float
    consecutive_sensor_failures: int
    events: tuple[DetectorEvent, ...] = ()

    @property
    def status(self) -> str:
        """Compatibility alias for earlier tests and callers."""
        return self.measurement_state

    @property
    def event(self) -> str | None:
        """Compatibility alias returning the first event type."""
        return self.events[0].event_type if self.events else None


class CurrentSpikeDetector:
    """Pure deterministic detector using monotonic nanoseconds."""

    def __init__(self, config: MonitorConfig) -> None:
        config.validate()
        self.config = config
        self.measurement_state = MEASUREMENT_STARTING
        self.red_flag_latched = False
        self._critical_since_ns: int | None = None
        self._critical_clear_since_ns: int | None = None
        self._critical_clear_samples = 0
        self._out_of_range_clear_samples = 0
        self._consecutive_sensor_failures = 0
        self._first_sensor_failure_ns: int | None = None
        self._sensor_error_active = False

    @staticmethod
    def _seconds_between(start_ns: int | None, end_ns: int) -> float:
        if start_ns is None:
            return 0.0
        return max(0, end_ns - start_ns) / 1_000_000_000.0

    def _base_classification(self, current_ma: int) -> str:
        if current_ma < self.config.normal_min_current_ma:
            return MEASUREMENT_LOW
        if current_ma <= self.config.normal_max_current_ma:
            return MEASUREMENT_NORMAL
        return MEASUREMENT_OUT_OF_RANGE

    def _set_state(
        self,
        new_state: str,
        events: list[DetectorEvent],
        *,
        current_ma: int | None = None,
        reason: str | None = None,
    ) -> None:
        old_state = self.measurement_state
        if old_state == new_state:
            return
        self.measurement_state = new_state
        details: dict[str, object] = {
            "previous_state": old_state,
            "new_state": new_state,
        }
        if current_ma is not None:
            details["current_ma"] = current_ma
        if reason is not None:
            details["reason"] = reason
        events.append(DetectorEvent("POWER_STATE_CHANGED", details))

    def _validate_current(self, current_ma: int) -> None:
        if isinstance(current_ma, bool) or not isinstance(current_ma, int):
            raise SensorDataError("current_ma must be an integer")
        if not (
            self.config.min_plausible_current_ma
            <= current_ma
            <= self.config.max_plausible_current_ma
        ):
            raise SensorDataError(
                f"current_ma={current_ma} is outside configured plausibility limits"
            )

    def update(
        self,
        current_ma: int | float,
        now_monotonic: int | float,
    ) -> DetectorResult:
        """Update the detector.

        Integer current values are interpreted as milliamperes. Floating-point
        values are accepted only for compatibility and interpreted as amperes.
        Integer monotonic values are nanoseconds. Floating-point values are
        accepted only for compatibility and interpreted as seconds.
        """
        if isinstance(current_ma, float):
            if not math.isfinite(current_ma):
                raise SensorDataError("current must be finite")
            normalized_current_ma = int(round(current_ma * 1000.0))
        else:
            normalized_current_ma = current_ma

        if isinstance(now_monotonic, float):
            if not math.isfinite(now_monotonic):
                raise ValueError("now_monotonic must be finite")
            now_ns = int(round(now_monotonic * 1_000_000_000.0))
        else:
            now_ns = now_monotonic

        if isinstance(now_ns, bool) or not isinstance(now_ns, int) or now_ns < 0:
            raise ValueError("now_monotonic must be a non-negative time value")

        return self.update_current(normalized_current_ma, now_ns)

    def update_current(self, current_ma: int, now_ns: int) -> DetectorResult:
        self._validate_current(current_ma)
        events: list[DetectorEvent] = []

        if self._consecutive_sensor_failures:
            events.append(
                DetectorEvent(
                    "POWER_SENSOR_RESTORED",
                    {
                        "previous_consecutive_failures": (
                            self._consecutive_sensor_failures
                        ),
                        "previous_sensor_error": self._sensor_error_active,
                        "current_ma": current_ma,
                    },
                )
            )
            self._consecutive_sensor_failures = 0
            self._first_sensor_failure_ns = None
            self._sensor_error_active = False

        above_seconds = self._seconds_between(self._critical_since_ns, now_ns)

        if current_ma >= self.config.critical_current_ma:
            self._critical_clear_since_ns = None
            self._critical_clear_samples = 0
            self._out_of_range_clear_samples = 0

            if self._critical_since_ns is None:
                self._critical_since_ns = now_ns
                above_seconds = 0.0
                events.append(
                    DetectorEvent(
                        "CURRENT_CRITICAL_THRESHOLD_ENTERED",
                        {
                            "current_ma": current_ma,
                            "critical_current_ma": (
                                self.config.critical_current_ma
                            ),
                        },
                    )
                )
            else:
                above_seconds = self._seconds_between(
                    self._critical_since_ns,
                    now_ns,
                )

            self._set_state(
                MEASUREMENT_TRIP_PENDING,
                events,
                current_ma=current_ma,
                reason="critical threshold active",
            )

            if (
                above_seconds >= self.config.critical_duration_seconds
                and not self.red_flag_latched
            ):
                self.red_flag_latched = True
                events.append(
                    DetectorEvent(
                        "CURRENT_RED_FLAG_LATCHED",
                        {
                            "current_ma": current_ma,
                            "critical_current_ma": (
                                self.config.critical_current_ma
                            ),
                            "required_duration_seconds": (
                                self.config.critical_duration_seconds
                            ),
                            "observed_duration_seconds": above_seconds,
                        },
                    )
                )

            return DetectorResult(
                measurement_state=self.measurement_state,
                red_flag_latched=self.red_flag_latched,
                above_critical_seconds=above_seconds,
                consecutive_sensor_failures=0,
                events=tuple(events),
            )

        if self._critical_since_ns is not None:
            if current_ma <= self.config.critical_clear_current_ma:
                if self._critical_clear_since_ns is None:
                    self._critical_clear_since_ns = now_ns
                    self._critical_clear_samples = 1
                else:
                    self._critical_clear_samples += 1

                clear_seconds = self._seconds_between(
                    self._critical_clear_since_ns,
                    now_ns,
                )
                clear_confirmed = (
                    clear_seconds
                    >= self.config.critical_clear_duration_seconds
                    and self._critical_clear_samples
                    >= self.config.critical_clear_consecutive_samples
                )
                if clear_confirmed:
                    previous_duration = self._seconds_between(
                        self._critical_since_ns,
                        now_ns,
                    )
                    self._critical_since_ns = None
                    self._critical_clear_since_ns = None
                    self._critical_clear_samples = 0
                    events.append(
                        DetectorEvent(
                            "CURRENT_CRITICAL_THRESHOLD_CLEARED",
                            {
                                "current_ma": current_ma,
                                "critical_clear_current_ma": (
                                    self.config.critical_clear_current_ma
                                ),
                                "previous_above_critical_seconds": (
                                    previous_duration
                                ),
                                "red_flag_latched": self.red_flag_latched,
                            },
                        )
                    )
                else:
                    self._set_state(
                        MEASUREMENT_TRIP_PENDING,
                        events,
                        current_ma=current_ma,
                        reason="critical clear debounce active",
                    )
                    return DetectorResult(
                        measurement_state=self.measurement_state,
                        red_flag_latched=self.red_flag_latched,
                        above_critical_seconds=above_seconds,
                        consecutive_sensor_failures=0,
                        events=tuple(events),
                    )
            else:
                self._critical_clear_since_ns = None
                self._critical_clear_samples = 0
                self._set_state(
                    MEASUREMENT_TRIP_PENDING,
                    events,
                    current_ma=current_ma,
                    reason="below entry threshold but above clear threshold",
                )
                return DetectorResult(
                    measurement_state=self.measurement_state,
                    red_flag_latched=self.red_flag_latched,
                    above_critical_seconds=above_seconds,
                    consecutive_sensor_failures=0,
                    events=tuple(events),
                )

        target_state = self._base_classification(current_ma)

        if target_state == MEASUREMENT_OUT_OF_RANGE:
            self._out_of_range_clear_samples = 0
            if self.measurement_state != MEASUREMENT_OUT_OF_RANGE:
                events.append(
                    DetectorEvent(
                        "CURRENT_OUT_OF_RANGE_ENTERED",
                        {
                            "current_ma": current_ma,
                            "normal_max_current_ma": (
                                self.config.normal_max_current_ma
                            ),
                        },
                    )
                )
            self._set_state(
                MEASUREMENT_OUT_OF_RANGE,
                events,
                current_ma=current_ma,
            )
        elif self.measurement_state == MEASUREMENT_OUT_OF_RANGE:
            if current_ma <= self.config.out_of_range_clear_current_ma:
                self._out_of_range_clear_samples += 1
            else:
                self._out_of_range_clear_samples = 0

            if (
                self._out_of_range_clear_samples
                >= self.config.out_of_range_clear_consecutive_samples
            ):
                self._out_of_range_clear_samples = 0
                events.append(
                    DetectorEvent(
                        "CURRENT_OUT_OF_RANGE_CLEARED",
                        {
                            "current_ma": current_ma,
                            "out_of_range_clear_current_ma": (
                                self.config.out_of_range_clear_current_ma
                            ),
                        },
                    )
                )
                self._set_state(
                    target_state,
                    events,
                    current_ma=current_ma,
                )
        else:
            self._out_of_range_clear_samples = 0
            self._set_state(target_state, events, current_ma=current_ma)

        return DetectorResult(
            measurement_state=self.measurement_state,
            red_flag_latched=self.red_flag_latched,
            above_critical_seconds=0.0,
            consecutive_sensor_failures=0,
            events=tuple(events),
        )

    def sensor_failure(
        self,
        now_ns: int,
        error_message: str,
    ) -> DetectorResult:
        """Record one failed sensor attempt and classify sensor health."""
        if now_ns < 0:
            raise ValueError("now_ns must be non-negative")

        events: list[DetectorEvent] = []
        self._consecutive_sensor_failures += 1
        if self._first_sensor_failure_ns is None:
            self._first_sensor_failure_ns = now_ns
            events.append(
                DetectorEvent(
                    "POWER_SENSOR_DEGRADED",
                    {
                        "error": error_message,
                        "consecutive_failures": 1,
                    },
                )
            )

        failure_seconds = self._seconds_between(
            self._first_sensor_failure_ns,
            now_ns,
        )
        becomes_error = (
            self._consecutive_sensor_failures
            >= self.config.sensor_error_consecutive_failures
            or failure_seconds >= self.config.sensor_error_timeout_seconds
        )

        if becomes_error:
            if not self._sensor_error_active:
                self._sensor_error_active = True
                events.append(
                    DetectorEvent(
                        "POWER_SENSOR_ERROR",
                        {
                            "error": error_message,
                            "consecutive_failures": (
                                self._consecutive_sensor_failures
                            ),
                            "failure_duration_seconds": failure_seconds,
                        },
                    )
                )
            self._set_state(
                MEASUREMENT_SENSOR_ERROR,
                events,
                reason="sensor error threshold reached",
            )
        else:
            self._set_state(
                MEASUREMENT_SENSOR_DEGRADED,
                events,
                reason="sensor read failed",
            )

        return DetectorResult(
            measurement_state=self.measurement_state,
            red_flag_latched=self.red_flag_latched,
            above_critical_seconds=self._seconds_between(
                self._critical_since_ns,
                now_ns,
            ),
            consecutive_sensor_failures=self._consecutive_sensor_failures,
            events=tuple(events),
        )

    def reset_red_flag(
        self,
        *,
        authorized: bool,
        active_run: bool,
    ) -> DetectorEvent:
        """Reset the diagnostic latch only through an authorized safe action."""
        if not authorized:
            raise PermissionError("Red-flag reset requires authorization")
        if self.config.prohibit_reset_during_active_run and active_run:
            raise RuntimeError(
                "Red-flag reset is prohibited during an active run"
            )

        was_latched = self.red_flag_latched
        self.red_flag_latched = False
        return DetectorEvent(
            "CURRENT_RED_FLAG_RESET",
            {"previously_latched": was_latched},
        )

    def stop(self) -> DetectorResult:
        events: list[DetectorEvent] = []
        self._set_state(MEASUREMENT_STOPPED, events, reason="monitor stopped")
        return DetectorResult(
            measurement_state=self.measurement_state,
            red_flag_latched=self.red_flag_latched,
            above_critical_seconds=0.0,
            consecutive_sensor_failures=self._consecutive_sensor_failures,
            events=tuple(events),
        )


class JsonlWriter:
    """Append compact JSON records with bounded synchronization overhead."""

    def __init__(
        self,
        path: Path,
        sync_interval_seconds: float = 1.0,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a", encoding="utf-8", newline="\n")
        self._sync_interval_seconds = sync_interval_seconds
        self._last_sync_monotonic = time.monotonic()

    def append(
        self,
        record: dict[str, object],
        *,
        force_sync: bool = False,
    ) -> None:
        serialized = json.dumps(
            record,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        self._file.write(serialized)
        self._file.write("\n")
        self._file.flush()

        now = time.monotonic()
        if (
            force_sync
            or now - self._last_sync_monotonic
            >= self._sync_interval_seconds
        ):
            os.fsync(self._file.fileno())
            self._last_sync_monotonic = now

    def close(self) -> None:
        if self._file.closed:
            return
        self._file.flush()
        os.fsync(self._file.fileno())
        self._file.close()

    def __enter__(self) -> "JsonlWriter":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


@dataclass(frozen=True)
class HwmonSensorFiles:
    current_file: Path | None
    voltage_file: Path | None
    power_file: Path | None
    label_file: Path

    @property
    def source_description(self) -> str:
        return str(self.label_file.parent)


class JetsonVddInSource:
    """Read one coherent VDD_IN observation through Linux hwmon."""

    def __init__(
        self,
        sensor_files: HwmonSensorFiles,
        config: MonitorConfig,
    ) -> None:
        if (
            sensor_files.current_file is None
            and (
                sensor_files.power_file is None
                or sensor_files.voltage_file is None
            )
        ):
            raise ValueError(
                "Current must be directly readable or derivable from power "
                "and voltage"
            )
        self.sensor_files = sensor_files
        self.config = config

    @staticmethod
    def _read_finite_number(path: Path) -> float:
        try:
            raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            raise
        try:
            value = float(raw)
        except ValueError as error:
            raise SensorDataError(
                f"Non-numeric sensor value in {path}: {raw!r}"
            ) from error
        if not math.isfinite(value):
            raise SensorDataError(f"Non-finite sensor value in {path}")
        return value

    def read(self) -> SensorReading:
        files = self.sensor_files

        raw_current_ma = (
            self._read_finite_number(files.current_file)
            if files.current_file is not None
            else None
        )
        raw_voltage_mv = (
            self._read_finite_number(files.voltage_file)
            if files.voltage_file is not None
            else None
        )
        raw_power_uw = (
            self._read_finite_number(files.power_file)
            if files.power_file is not None
            else None
        )

        flags: list[str] = []

        if raw_current_ma is not None:
            current_ma = int(round(raw_current_ma))
        else:
            assert raw_power_uw is not None
            assert raw_voltage_mv is not None
            if raw_voltage_mv == 0:
                raise SensorDataError("VDD_IN voltage reading is zero")
            current_ma = int(round(raw_power_uw / raw_voltage_mv))
            flags.append("CURRENT_DERIVED_FROM_POWER_AND_VOLTAGE")

        voltage_mv = (
            None if raw_voltage_mv is None else int(round(raw_voltage_mv))
        )
        power_mw = (
            None if raw_power_uw is None else int(round(raw_power_uw / 1000.0))
        )

        if power_mw is None and voltage_mv is not None:
            power_mw = int(round((voltage_mv * current_ma) / 1000.0))
            flags.append("POWER_DERIVED_FROM_CURRENT_AND_VOLTAGE")

        self._validate_reading(current_ma, voltage_mv, power_mw)

        return SensorReading(
            current_ma=current_ma,
            voltage_mv=voltage_mv,
            power_mw=power_mw,
            sensor_source=files.source_description,
            data_quality_flags=tuple(flags),
        )

    def _validate_reading(
        self,
        current_ma: int,
        voltage_mv: int | None,
        power_mw: int | None,
    ) -> None:
        if not (
            self.config.min_plausible_current_ma
            <= current_ma
            <= self.config.max_plausible_current_ma
        ):
            raise SensorDataError(
                f"Implausible current reading: {current_ma} mA"
            )
        if voltage_mv is not None and not (
            self.config.min_plausible_voltage_mv
            <= voltage_mv
            <= self.config.max_plausible_voltage_mv
        ):
            raise SensorDataError(
                f"Implausible voltage reading: {voltage_mv} mV"
            )
        if power_mw is not None and not (
            0 <= power_mw <= self.config.max_plausible_power_mw
        ):
            raise SensorDataError(
                f"Implausible power reading: {power_mw} mW"
            )


def find_vdd_in_sensor() -> HwmonSensorFiles:
    """Locate a readable Jetson INA3221 channel labeled VDD_IN."""
    candidate_patterns = (
        "/sys/bus/i2c/drivers/ina3221/1-0040/hwmon/hwmon*",
        "/sys/class/hwmon/hwmon*",
    )

    directories: list[Path] = []
    seen: set[Path] = set()
    for pattern in candidate_patterns:
        for item in glob.glob(pattern):
            directory = Path(item)
            try:
                resolved = directory.resolve()
            except OSError:
                continue
            if resolved not in seen:
                seen.add(resolved)
                directories.append(directory)

    for directory in directories:
        for label_file in directory.glob("in*_label"):
            try:
                label = label_file.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if label != "VDD_IN":
                continue

            match = re.fullmatch(r"in(\d+)_label", label_file.name)
            if match is None:
                continue

            index = match.group(1)
            current_file = directory / f"curr{index}_input"
            voltage_file = directory / f"in{index}_input"
            power_file = directory / f"power{index}_input"

            current = current_file if current_file.is_file() else None
            voltage = voltage_file if voltage_file.is_file() else None
            power = power_file if power_file.is_file() else None

            if current is not None or (
                power is not None and voltage is not None
            ):
                return HwmonSensorFiles(
                    current_file=current,
                    voltage_file=voltage,
                    power_file=power,
                    label_file=label_file,
                )

    raise FileNotFoundError(
        "Could not locate a readable VDD_IN INA3221 hwmon channel"
    )


class SimulatedCurrentSource:
    """Repeat normal, warning, critical, and recovery measurements."""

    def __init__(self) -> None:
        self._started_ns = time.monotonic_ns()

    def read(self) -> SensorReading:
        elapsed = ((time.monotonic_ns() - self._started_ns) / 1e9) % 20.0
        if elapsed < 5.0:
            current_ma = 2000
        elif elapsed < 8.0:
            current_ma = 2200
        elif elapsed < 12.0:
            current_ma = 2350
        else:
            current_ma = 2000

        voltage_mv = 12000
        power_mw = int(round((voltage_mv * current_ma) / 1000.0))
        return SensorReading(
            current_ma=current_ma,
            voltage_mv=voltage_mv,
            power_mw=power_mw,
            sensor_source="simulator",
            data_quality_flags=("SIMULATED",),
        )


@dataclass(frozen=True)
class MonitorIdentity:
    monitor_session_id: str
    run_id: str
    jetson_id: str
    boot_id: str
    software_version: str
    git_commit: str
    configuration_fingerprint: str


class RecordFactory:
    """Build records with one common schema envelope and sequence."""

    def __init__(self, identity: MonitorIdentity) -> None:
        self.identity = identity
        self._sequence = 0

    def create(
        self,
        *,
        record_type: str,
        event_type: str | None,
        monotonic_ns: int,
        payload: dict[str, object],
    ) -> dict[str, object]:
        self._sequence += 1
        record: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "record_type": record_type,
            "event_type": event_type,
            "event_id": str(uuid.uuid4()) if event_type else None,
            "sequence": self._sequence,
            "recorded_at_utc": utc_timestamp(),
            "monotonic_ns": monotonic_ns,
            "monitor_session_id": self.identity.monitor_session_id,
            "run_id": self.identity.run_id,
            "jetson_id": self.identity.jetson_id,
            "boot_id": self.identity.boot_id,
            "configuration_fingerprint": (
                self.identity.configuration_fingerprint
            ),
            "software_version": self.identity.software_version,
            "git_commit": self.identity.git_commit,
        }
        record.update(payload)
        return record


def default_log_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.cwd() / "logs" / "power" / f"power_monitor_{timestamp}.jsonl"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read Jetson VDD_IN telemetry and record diagnostic power events."
        )
    )
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--log-file", type=Path, default=None)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--max-samples", type=int, default=None)
    parser.add_argument("--run-id", default="unassigned")
    parser.add_argument("--jetson-id", default=socket.gethostname())

    # Backward-compatible command-line overrides.
    parser.add_argument("--nominal-current-a", type=float, default=None)
    parser.add_argument("--nominal-tolerance-a", type=float, default=None)
    parser.add_argument("--trip-threshold-a", type=float, default=None)
    parser.add_argument("--trip-duration-seconds", type=float, default=None)
    parser.add_argument("--sample-interval-seconds", type=float, default=None)
    parser.add_argument("--sync-interval-seconds", type=float, default=None)
    return parser


def load_config(args: argparse.Namespace) -> MonitorConfig:
    config = (
        MonitorConfig.from_json_file(args.config)
        if args.config is not None
        else MonitorConfig()
    )

    updates: dict[str, object] = {}
    if args.nominal_current_a is not None:
        center_ma = int(round(args.nominal_current_a * 1000.0))
        tolerance_a = (
            args.nominal_tolerance_a
            if args.nominal_tolerance_a is not None
            else (
                config.normal_max_current_ma
                - config.normal_min_current_ma
            )
            / 2000.0
        )
        tolerance_ma = int(round(tolerance_a * 1000.0))
        updates["normal_min_current_ma"] = center_ma - tolerance_ma
        updates["normal_max_current_ma"] = center_ma + tolerance_ma
    elif args.nominal_tolerance_a is not None:
        raise ConfigurationError(
            "--nominal-tolerance-a requires --nominal-current-a"
        )

    if args.trip_threshold_a is not None:
        updates["critical_current_ma"] = int(
            round(args.trip_threshold_a * 1000.0)
        )
    if args.trip_duration_seconds is not None:
        updates["critical_duration_seconds"] = args.trip_duration_seconds
    if args.sample_interval_seconds is not None:
        updates["sample_interval_seconds"] = args.sample_interval_seconds
    if args.sync_interval_seconds is not None:
        updates["sync_interval_seconds"] = args.sync_interval_seconds

    config = replace(config, **updates)
    config.validate()
    return config


def _event_payload(
    result: DetectorResult,
    event: DetectorEvent,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "measurement_state": result.measurement_state,
        "red_flag_latched": result.red_flag_latched,
        "above_critical_seconds": round(
            result.above_critical_seconds,
            6,
        ),
        "consecutive_sensor_failures": (
            result.consecutive_sensor_failures
        ),
    }
    payload.update(event.details)
    return payload


def run_monitor(
    source: CurrentSource,
    config: MonitorConfig,
    log_path: Path,
    *,
    identity: MonitorIdentity | None = None,
    max_samples: int | None = None,
) -> int:
    """Run the monitor until interrupted or the attempt limit is reached."""
    config.validate()
    if max_samples is not None and max_samples <= 0:
        raise ValueError("max_samples must be positive")

    identity = identity or MonitorIdentity(
        monitor_session_id=str(uuid.uuid4()),
        run_id="unassigned",
        jetson_id=socket.gethostname(),
        boot_id=read_boot_id(),
        software_version=SOFTWARE_VERSION,
        git_commit=detect_git_commit(),
        configuration_fingerprint=config.fingerprint(),
    )
    records = RecordFactory(identity)
    detector = CurrentSpikeDetector(config)
    attempt_count = 0
    successful_sample_count = 0
    previous_cycle_started_ns: int | None = None
    exit_code = 0

    with JsonlWriter(log_path, config.sync_interval_seconds) as writer:
        started_ns = time.monotonic_ns()
        writer.append(
            records.create(
                record_type="monitor_startup",
                event_type="POWER_MONITOR_STARTED",
                monotonic_ns=started_ns,
                payload={
                    "measurement_state": detector.measurement_state,
                    "red_flag_latched": detector.red_flag_latched,
                    "response_mode": config.response_mode,
                    "source_type": type(source).__name__,
                    "log_file": str(log_path),
                    "config": asdict(config),
                },
            ),
            force_sync=True,
        )
        print(f"Power monitor log: {log_path}", flush=True)

        try:
            while max_samples is None or attempt_count < max_samples:
                cycle_started_ns = time.monotonic_ns()
                attempt_count += 1
                actual_interval_ms = (
                    None
                    if previous_cycle_started_ns is None
                    else round(
                        (cycle_started_ns - previous_cycle_started_ns)
                        / 1_000_000.0,
                        3,
                    )
                )
                previous_cycle_started_ns = cycle_started_ns

                try:
                    reading = source.read()
                    result = detector.update_current(
                        reading.current_ma,
                        cycle_started_ns,
                    )
                except (
                    OSError,
                    SensorDataError,
                    ValueError,
                    ZeroDivisionError,
                ) as error:
                    result = detector.sensor_failure(
                        cycle_started_ns,
                        str(error),
                    )
                    for event in result.events:
                        event_record = records.create(
                            record_type="sensor_health_event",
                            event_type=event.event_type,
                            monotonic_ns=cycle_started_ns,
                            payload=_event_payload(result, event),
                        )
                        writer.append(event_record, force_sync=True)
                        print(json.dumps(event_record, indent=2), flush=True)

                    elapsed_seconds = (
                        time.monotonic_ns() - cycle_started_ns
                    ) / 1e9
                    remaining = config.sample_interval_seconds - elapsed_seconds
                    if remaining > 0:
                        time.sleep(remaining)
                    continue

                successful_sample_count += 1
                loop_processing_ms = round(
                    (time.monotonic_ns() - cycle_started_ns) / 1_000_000.0,
                    3,
                )
                sample_record = records.create(
                    record_type="power_sample",
                    event_type=None,
                    monotonic_ns=cycle_started_ns,
                    payload={
                        "current_ma": reading.current_ma,
                        "voltage_mv": reading.voltage_mv,
                        "power_mw": reading.power_mw,
                        "current_a": reading.current_a,
                        "voltage_v": reading.voltage_v,
                        "power_w": reading.power_w,
                        "measurement_state": result.measurement_state,
                        "red_flag_latched": result.red_flag_latched,
                        "above_critical_seconds": round(
                            result.above_critical_seconds,
                            6,
                        ),
                        "sensor_source": reading.sensor_source,
                        "configured_sample_interval_ms": round(
                            config.sample_interval_seconds * 1000.0,
                            3,
                        ),
                        "actual_sample_interval_ms": actual_interval_ms,
                        "loop_processing_ms": loop_processing_ms,
                        "loop_overrun": (
                            loop_processing_ms
                            > config.sample_interval_seconds * 1000.0
                        ),
                        "data_quality_flags": list(
                            reading.data_quality_flags
                        ),
                    },
                )
                writer.append(sample_record)

                for event in result.events:
                    event_record = records.create(
                        record_type="power_state_event",
                        event_type=event.event_type,
                        monotonic_ns=cycle_started_ns,
                        payload=_event_payload(result, event),
                    )
                    writer.append(event_record, force_sync=True)
                    print(json.dumps(event_record, indent=2), flush=True)

                elapsed_seconds = (
                    time.monotonic_ns() - cycle_started_ns
                ) / 1e9
                remaining = config.sample_interval_seconds - elapsed_seconds
                if remaining > 0:
                    time.sleep(remaining)

        except KeyboardInterrupt:
            print("\nPower monitor stopped.", flush=True)
        except OSError as error:
            # A log/filesystem failure cannot be safely recorded in the same
            # failed destination. Surface it to stderr and return non-zero.
            print(f"POWER_MONITOR_IO_FAILURE: {error}", file=sys.stderr)
            exit_code = 2
        finally:
            stopped_ns = time.monotonic_ns()
            stopped = detector.stop()
            try:
                for event in stopped.events:
                    writer.append(
                        records.create(
                            record_type="power_state_event",
                            event_type=event.event_type,
                            monotonic_ns=stopped_ns,
                            payload=_event_payload(stopped, event),
                        ),
                        force_sync=True,
                    )
                writer.append(
                    records.create(
                        record_type="monitor_shutdown",
                        event_type="POWER_MONITOR_STOPPED",
                        monotonic_ns=stopped_ns,
                        payload={
                            "measurement_state": stopped.measurement_state,
                            "red_flag_latched": stopped.red_flag_latched,
                            "attempt_count": attempt_count,
                            "successful_sample_count": (
                                successful_sample_count
                            ),
                            "exit_code": exit_code,
                        },
                    ),
                    force_sync=True,
                )
            except OSError as error:
                print(
                    f"POWER_MONITOR_SHUTDOWN_LOG_FAILURE: {error}",
                    file=sys.stderr,
                )
                exit_code = 2

    return exit_code


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(args)

    if args.max_samples is not None and args.max_samples <= 0:
        raise ConfigurationError("--max-samples must be positive")

    identity = MonitorIdentity(
        monitor_session_id=str(uuid.uuid4()),
        run_id=args.run_id,
        jetson_id=args.jetson_id,
        boot_id=read_boot_id(),
        software_version=SOFTWARE_VERSION,
        git_commit=detect_git_commit(),
        configuration_fingerprint=config.fingerprint(),
    )
    log_path = args.log_file or default_log_path()

    if args.simulate:
        source: CurrentSource = SimulatedCurrentSource()
    else:
        source = JetsonVddInSource(find_vdd_in_sensor(), config)

    return run_monitor(
        source,
        config,
        log_path,
        identity=identity,
        max_samples=args.max_samples,
    )


if __name__ == "__main__":
    raise SystemExit(main())
