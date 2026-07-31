#!/usr/bin/env python3
"""Jetson VDD_IN current monitor with persistent JSONL logging.

Stage-1 behavior:
- Treat 2.0 A ± 0.1 A as the nominal band.
- Treat current from above 2.1 A to below 2.3 A as out of range.
- Raise a RED_FLAG only when current is continuously >= 2.3 A
  for at least 3.0 seconds.
- Save every sample and every state transition to a JSONL file.

This program does not remove electrical power. It detects and records
the condition so protective hardware can be integrated later.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


def utc_timestamp() -> str:
    """Return a UTC timestamp in ISO 8601 format."""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class MonitorConfig:
    nominal_current_a: float = 2.0
    nominal_tolerance_a: float = 0.1
    trip_threshold_a: float = 2.3
    trip_duration_seconds: float = 3.0
    sample_interval_seconds: float = 0.2
    sync_interval_seconds: float = 1.0

    @property
    def nominal_min_a(self) -> float:
        return self.nominal_current_a - self.nominal_tolerance_a

    @property
    def nominal_max_a(self) -> float:
        return self.nominal_current_a + self.nominal_tolerance_a

    def validate(self) -> None:
        if self.nominal_current_a <= 0:
            raise ValueError("nominal_current_a must be positive")
        if self.nominal_tolerance_a < 0:
            raise ValueError("nominal_tolerance_a cannot be negative")
        if self.trip_threshold_a <= self.nominal_max_a:
            raise ValueError(
                "trip_threshold_a must be above the nominal upper limit"
            )
        if self.trip_duration_seconds <= 0:
            raise ValueError("trip_duration_seconds must be positive")
        if self.sample_interval_seconds <= 0:
            raise ValueError("sample_interval_seconds must be positive")
        if self.sync_interval_seconds <= 0:
            raise ValueError("sync_interval_seconds must be positive")


@dataclass(frozen=True)
class SensorReading:
    current_a: float
    voltage_v: float | None = None
    power_w: float | None = None
    sensor_source: str = "unknown"


class CurrentSource(Protocol):
    def read(self) -> SensorReading:
        """Return one current reading."""


@dataclass(frozen=True)
class DetectorResult:
    status: str
    above_trip_seconds: float
    event: str | None = None
    event_details: dict[str, object] | None = None


class CurrentSpikeDetector:
    """Detect a sustained overcurrent condition using monotonic time."""

    def __init__(self, config: MonitorConfig) -> None:
        config.validate()
        self.config = config
        self._above_since: float | None = None
        self._red_flag_active = False

    def classify_without_trip(self, current_a: float) -> str:
        if current_a < self.config.nominal_min_a:
            return "LOW"
        if current_a <= self.config.nominal_max_a:
            return "NORMAL"
        return "OUT_OF_RANGE"

    def update(self, current_a: float, now_monotonic: float) -> DetectorResult:
        threshold = self.config.trip_threshold_a

        if current_a >= threshold:
            if self._above_since is None:
                self._above_since = now_monotonic
                return DetectorResult(
                    status="TRIP_PENDING",
                    above_trip_seconds=0.0,
                    event="CURRENT_THRESHOLD_ENTERED",
                    event_details={
                        "current_a": current_a,
                        "trip_threshold_a": threshold,
                    },
                )

            elapsed = max(0.0, now_monotonic - self._above_since)

            if (
                elapsed >= self.config.trip_duration_seconds
                and not self._red_flag_active
            ):
                self._red_flag_active = True
                return DetectorResult(
                    status="RED_FLAG",
                    above_trip_seconds=elapsed,
                    event="CURRENT_RED_FLAG",
                    event_details={
                        "current_a": current_a,
                        "trip_threshold_a": threshold,
                        "required_duration_seconds": (
                            self.config.trip_duration_seconds
                        ),
                        "observed_duration_seconds": elapsed,
                    },
                )

            return DetectorResult(
                status=(
                    "RED_FLAG"
                    if self._red_flag_active
                    else "TRIP_PENDING"
                ),
                above_trip_seconds=elapsed,
            )

        previous_above_since = self._above_since
        was_red = self._red_flag_active

        self._above_since = None
        self._red_flag_active = False

        if previous_above_since is not None:
            elapsed = max(0.0, now_monotonic - previous_above_since)
            return DetectorResult(
                status=self.classify_without_trip(current_a),
                above_trip_seconds=0.0,
                event="CURRENT_THRESHOLD_CLEARED",
                event_details={
                    "current_a": current_a,
                    "previously_red_flag": was_red,
                    "previous_above_trip_seconds": elapsed,
                },
            )

        return DetectorResult(
            status=self.classify_without_trip(current_a),
            above_trip_seconds=0.0,
        )


class JsonlWriter:
    """Append JSON records and periodically synchronize them to storage."""

    def __init__(
        self,
        path: Path,
        sync_interval_seconds: float = 1.0,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open(
            mode="a",
            encoding="utf-8",
            newline="\n",
        )
        self._sync_interval_seconds = sync_interval_seconds
        self._last_sync_monotonic = time.monotonic()

    def append(
        self,
        record: dict[str, object],
        *,
        force_sync: bool = False,
    ) -> None:
        self._file.write(
            json.dumps(
                record,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        )
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
    """Read the Jetson INA3221 VDD_IN channel through Linux hwmon."""

    def __init__(self, sensor_files: HwmonSensorFiles) -> None:
        if (
            sensor_files.current_file is None
            and (
                sensor_files.power_file is None
                or sensor_files.voltage_file is None
            )
        ):
            raise ValueError(
                "Current must be directly readable or derivable "
                "from power and voltage"
            )
        self.sensor_files = sensor_files

    @staticmethod
    def _read_number(path: Path) -> float:
        return float(path.read_text(encoding="utf-8").strip())

    def read(self) -> SensorReading:
        files = self.sensor_files

        voltage_v: float | None = None
        power_w: float | None = None

        if files.voltage_file is not None:
            voltage_v = self._read_number(files.voltage_file) / 1000.0

        if files.current_file is not None:
            current_a = self._read_number(files.current_file) / 1000.0
        else:
            assert files.power_file is not None
            assert files.voltage_file is not None
            raw_power_uw = self._read_number(files.power_file)
            raw_voltage_mv = self._read_number(files.voltage_file)
            if raw_voltage_mv == 0:
                raise ZeroDivisionError("VDD_IN voltage reading is zero")
            current_a = (raw_power_uw / raw_voltage_mv) / 1000.0

        if files.power_file is not None:
            power_w = self._read_number(files.power_file) / 1_000_000.0
        elif voltage_v is not None:
            power_w = voltage_v * current_a

        return SensorReading(
            current_a=current_a,
            voltage_v=voltage_v,
            power_w=power_w,
            sensor_source=files.source_description,
        )


def find_vdd_in_sensor() -> HwmonSensorFiles:
    """Locate the Jetson VDD_IN INA3221 hwmon channel."""
    candidate_patterns = (
        "/sys/bus/i2c/drivers/ina3221/1-0040/hwmon/hwmon*",
        "/sys/class/hwmon/hwmon*",
    )

    directories: list[Path] = []
    seen: set[Path] = set()

    for pattern in candidate_patterns:
        for item in glob.glob(pattern):
            directory = Path(item)
            resolved = directory.resolve()
            if resolved not in seen:
                seen.add(resolved)
                directories.append(directory)

    for directory in directories:
        for label_file in directory.glob("in*_label"):
            try:
                label = label_file.read_text(
                    encoding="utf-8"
                ).strip()
            except OSError:
                continue

            if label != "VDD_IN":
                continue

            match = re.fullmatch(
                r"in(\d+)_label",
                label_file.name,
            )
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
    """Cycle through normal, out-of-range, sustained-trip and recovery states."""

    def __init__(self) -> None:
        self._started = time.monotonic()

    def read(self) -> SensorReading:
        elapsed = (time.monotonic() - self._started) % 20.0

        if elapsed < 5.0:
            current_a = 2.0
        elif elapsed < 8.0:
            current_a = 2.2
        elif elapsed < 12.0:
            current_a = 2.35
        else:
            current_a = 2.0

        voltage_v = 19.5
        return SensorReading(
            current_a=current_a,
            voltage_v=voltage_v,
            power_w=voltage_v * current_a,
            sensor_source="simulator",
        )


def default_log_path() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return (
        Path.cwd()
        / "logs"
        / "power"
        / f"power_monitor_{timestamp}.jsonl"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Monitor Jetson VDD_IN current and flag a sustained "
            "overcurrent condition."
        )
    )
    parser.add_argument(
        "--nominal-current-a",
        type=float,
        default=2.0,
    )
    parser.add_argument(
        "--nominal-tolerance-a",
        type=float,
        default=0.1,
    )
    parser.add_argument(
        "--trip-threshold-a",
        type=float,
        default=2.3,
    )
    parser.add_argument(
        "--trip-duration-seconds",
        type=float,
        default=3.0,
    )
    parser.add_argument(
        "--sample-interval-seconds",
        type=float,
        default=0.2,
    )
    parser.add_argument(
        "--sync-interval-seconds",
        type=float,
        default=1.0,
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Use a repeating simulated current profile.",
    )
    parser.add_argument(
        "--max-samples",
        type=int,
        default=None,
        help="Stop after this many samples; default is to run forever.",
    )
    return parser


def run_monitor(
    source: CurrentSource,
    config: MonitorConfig,
    log_path: Path,
    max_samples: int | None = None,
) -> int:
    detector = CurrentSpikeDetector(config)
    sample_count = 0

    with JsonlWriter(
        log_path,
        sync_interval_seconds=config.sync_interval_seconds,
    ) as writer:
        writer.append(
            {
                "record_type": "event",
                "event": "POWER_MONITOR_STARTED",
                "recorded_at_utc": utc_timestamp(),
                "config": asdict(config),
                "log_file": str(log_path),
            },
            force_sync=True,
        )

        print(f"Power monitor log: {log_path}", flush=True)

        try:
            while (
                max_samples is None
                or sample_count < max_samples
            ):
                cycle_started = time.monotonic()

                try:
                    reading = source.read()
                except (OSError, ValueError, ZeroDivisionError) as error:
                    writer.append(
                        {
                            "record_type": "event",
                            "event": "POWER_SENSOR_READ_FAILED",
                            "recorded_at_utc": utc_timestamp(),
                            "error": str(error),
                        },
                        force_sync=True,
                    )
                    print(
                        f"POWER_SENSOR_READ_FAILED: {error}",
                        file=sys.stderr,
                        flush=True,
                    )
                    time.sleep(config.sample_interval_seconds)
                    continue

                result = detector.update(
                    reading.current_a,
                    cycle_started,
                )

                writer.append(
                    {
                        "record_type": "sample",
                        "recorded_at_utc": utc_timestamp(),
                        "current_a": round(reading.current_a, 6),
                        "voltage_v": (
                            None
                            if reading.voltage_v is None
                            else round(reading.voltage_v, 6)
                        ),
                        "power_w": (
                            None
                            if reading.power_w is None
                            else round(reading.power_w, 6)
                        ),
                        "status": result.status,
                        "above_trip_seconds": round(
                            result.above_trip_seconds,
                            3,
                        ),
                        "nominal_min_a": config.nominal_min_a,
                        "nominal_max_a": config.nominal_max_a,
                        "trip_threshold_a": (
                            config.trip_threshold_a
                        ),
                        "trip_duration_seconds": (
                            config.trip_duration_seconds
                        ),
                        "sensor_source": reading.sensor_source,
                    },
                    force_sync=(result.status == "RED_FLAG"),
                )

                if result.event is not None:
                    event_record: dict[str, object] = {
                        "record_type": "event",
                        "event": result.event,
                        "recorded_at_utc": utc_timestamp(),
                        "status": result.status,
                    }
                    if result.event_details:
                        event_record.update(result.event_details)

                    writer.append(
                        event_record,
                        force_sync=True,
                    )
                    print(
                        json.dumps(
                            event_record,
                            indent=2,
                        ),
                        flush=True,
                    )

                sample_count += 1

                elapsed = time.monotonic() - cycle_started
                remaining = (
                    config.sample_interval_seconds - elapsed
                )
                if remaining > 0:
                    time.sleep(remaining)

        except KeyboardInterrupt:
            print("\nPower monitor stopped.", flush=True)

        finally:
            writer.append(
                {
                    "record_type": "event",
                    "event": "POWER_MONITOR_STOPPED",
                    "recorded_at_utc": utc_timestamp(),
                    "sample_count": sample_count,
                },
                force_sync=True,
            )

    return 0


def main() -> int:
    args = build_parser().parse_args()

    config = MonitorConfig(
        nominal_current_a=args.nominal_current_a,
        nominal_tolerance_a=args.nominal_tolerance_a,
        trip_threshold_a=args.trip_threshold_a,
        trip_duration_seconds=args.trip_duration_seconds,
        sample_interval_seconds=args.sample_interval_seconds,
        sync_interval_seconds=args.sync_interval_seconds,
    )
    config.validate()

    if args.max_samples is not None and args.max_samples <= 0:
        raise ValueError("--max-samples must be positive")

    log_path = args.log_file or default_log_path()

    if args.simulate:
        source: CurrentSource = SimulatedCurrentSource()
    else:
        source = JetsonVddInSource(
            find_vdd_in_sensor()
        )

    return run_monitor(
        source=source,
        config=config,
        log_path=log_path,
        max_samples=args.max_samples,
    )


if __name__ == "__main__":
    raise SystemExit(main())
