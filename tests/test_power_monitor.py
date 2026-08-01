"""Unit and small integration tests for power_monitor.py."""

from __future__ import annotations

import io
import json
import math
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from power_monitor import (
    ConfigurationError,
    CurrentSpikeDetector,
    HwmonSensorFiles,
    JetsonVddInSource,
    MonitorConfig,
    MonitorIdentity,
    SensorDataError,
    SensorReading,
    run_monitor,
)


def event_types(result: object) -> list[str]:
    return [event.event_type for event in result.events]


class TestMonitorConfig(unittest.TestCase):
    def test_example_boundaries_validate(self) -> None:
        MonitorConfig().validate()

    def test_unknown_configuration_field_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            MonitorConfig.from_mapping({"not_a_real_field": 1})

    def test_unsafe_response_mode_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            MonitorConfig.from_mapping({"response_mode": "cut_power"})

    def test_latch_cannot_be_disabled_in_this_stage(self) -> None:
        with self.assertRaises(ConfigurationError):
            MonitorConfig.from_mapping({"latch_red_flag": False})

    def test_json_configuration_loads(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "normal_min_current_ma": 1800,
                        "normal_max_current_ma": 2200,
                        "out_of_range_clear_current_ma": 2100,
                        "critical_current_ma": 2400,
                        "critical_clear_current_ma": 2300,
                    }
                ),
                encoding="utf-8",
            )
            config = MonitorConfig.from_json_file(path)
        self.assertEqual(config.normal_min_current_ma, 1800)
        self.assertEqual(config.critical_current_ma, 2400)


class TestCurrentSpikeDetector(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MonitorConfig()
        self.detector = CurrentSpikeDetector(self.config)

    def test_exact_normal_boundaries_are_normal(self) -> None:
        self.assertEqual(
            self.detector.update_current(1900, 0).measurement_state,
            "NORMAL",
        )
        self.assertEqual(
            self.detector.update_current(2100, 1_000_000_000).measurement_state,
            "NORMAL",
        )

    def test_values_outside_normal_band(self) -> None:
        self.assertEqual(
            self.detector.update_current(1899, 0).measurement_state,
            "LOW",
        )
        result = self.detector.update_current(2101, 1_000_000_000)
        self.assertEqual(result.measurement_state, "OUT_OF_RANGE")
        self.assertIn("CURRENT_OUT_OF_RANGE_ENTERED", event_types(result))

    def test_2299_is_out_of_range_and_2300_starts_critical_timer(self) -> None:
        below = self.detector.update_current(2299, 0)
        self.assertEqual(below.measurement_state, "OUT_OF_RANGE")

        entered = self.detector.update_current(2300, 1_000_000_000)
        self.assertEqual(entered.measurement_state, "TRIP_PENDING")
        self.assertIn(
            "CURRENT_CRITICAL_THRESHOLD_ENTERED",
            event_types(entered),
        )

    def test_three_seconds_latches_red_flag(self) -> None:
        self.detector.update_current(2300, 10_000_000_000)
        pending = self.detector.update_current(2350, 12_999_999_999)
        self.assertFalse(pending.red_flag_latched)

        red = self.detector.update_current(2350, 13_000_000_000)
        self.assertTrue(red.red_flag_latched)
        self.assertIn("CURRENT_RED_FLAG_LATCHED", event_types(red))

    def test_single_below_threshold_sample_does_not_reset_timer(self) -> None:
        self.detector.update_current(2350, 0)
        dip = self.detector.update_current(2240, 1_000_000_000)
        self.assertEqual(dip.measurement_state, "TRIP_PENDING")

        red = self.detector.update_current(2350, 3_000_000_000)
        self.assertTrue(red.red_flag_latched)

    def test_critical_clear_requires_time_and_three_samples(self) -> None:
        self.detector.update_current(2350, 0)
        first = self.detector.update_current(2200, 100_000_000)
        second = self.detector.update_current(2200, 400_000_000)
        self.assertEqual(first.measurement_state, "TRIP_PENDING")
        self.assertEqual(second.measurement_state, "TRIP_PENDING")

        cleared = self.detector.update_current(2200, 600_000_000)
        self.assertEqual(cleared.measurement_state, "OUT_OF_RANGE")
        self.assertIn(
            "CURRENT_CRITICAL_THRESHOLD_CLEARED",
            event_types(cleared),
        )

    def test_out_of_range_clear_requires_three_samples_at_or_below_2050(self) -> None:
        self.detector.update_current(2200, 0)
        self.assertEqual(
            self.detector.update_current(2050, 1).measurement_state,
            "OUT_OF_RANGE",
        )
        self.assertEqual(
            self.detector.update_current(2050, 2).measurement_state,
            "OUT_OF_RANGE",
        )
        cleared = self.detector.update_current(2050, 3)
        self.assertEqual(cleared.measurement_state, "NORMAL")
        self.assertIn("CURRENT_OUT_OF_RANGE_CLEARED", event_types(cleared))

    def test_red_flag_remains_latched_after_measurement_recovers(self) -> None:
        self.detector.update_current(2350, 0)
        red = self.detector.update_current(2350, 3_000_000_000)
        self.assertTrue(red.red_flag_latched)

        self.detector.update_current(2000, 3_100_000_000)
        self.detector.update_current(2000, 3_400_000_000)
        recovered = self.detector.update_current(2000, 3_600_000_000)
        self.assertEqual(recovered.measurement_state, "NORMAL")
        self.assertTrue(recovered.red_flag_latched)

    def test_reset_requires_authorization_and_no_active_run(self) -> None:
        self.detector.update_current(2350, 0)
        self.detector.update_current(2350, 3_000_000_000)

        with self.assertRaises(PermissionError):
            self.detector.reset_red_flag(
                authorized=False,
                active_run=False,
            )
        with self.assertRaises(RuntimeError):
            self.detector.reset_red_flag(
                authorized=True,
                active_run=True,
            )

        event = self.detector.reset_red_flag(
            authorized=True,
            active_run=False,
        )
        self.assertEqual(event.event_type, "CURRENT_RED_FLAG_RESET")
        self.assertFalse(self.detector.red_flag_latched)

    def test_sensor_degraded_error_and_restored(self) -> None:
        first = self.detector.sensor_failure(0, "read failed")
        self.assertEqual(first.measurement_state, "SENSOR_DEGRADED")
        self.assertIn("POWER_SENSOR_DEGRADED", event_types(first))

        second = self.detector.sensor_failure(200_000_000, "read failed")
        self.assertEqual(second.measurement_state, "SENSOR_DEGRADED")

        third = self.detector.sensor_failure(400_000_000, "read failed")
        self.assertEqual(third.measurement_state, "SENSOR_ERROR")
        self.assertIn("POWER_SENSOR_ERROR", event_types(third))

        restored = self.detector.update_current(2000, 500_000_000)
        self.assertEqual(restored.measurement_state, "NORMAL")
        self.assertIn("POWER_SENSOR_RESTORED", event_types(restored))

    def test_non_finite_compatibility_input_is_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf):
            with self.subTest(value=value):
                with self.assertRaises(SensorDataError):
                    self.detector.update(value, 0.0)

    def test_implausible_current_is_rejected(self) -> None:
        with self.assertRaises(SensorDataError):
            self.detector.update_current(10001, 0)


class TestJetsonVddInSource(unittest.TestCase):
    def test_direct_current_is_preferred_and_files_are_read_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            label = root / "in1_label"
            current = root / "curr1_input"
            voltage = root / "in1_input"
            power = root / "power1_input"
            label.write_text("VDD_IN\n", encoding="utf-8")
            current.write_text("2000\n", encoding="utf-8")
            voltage.write_text("12000\n", encoding="utf-8")
            power.write_text("24000000\n", encoding="utf-8")

            source = JetsonVddInSource(
                HwmonSensorFiles(current, voltage, power, label),
                MonitorConfig(),
            )
            reading = source.read()

        self.assertEqual(reading.current_ma, 2000)
        self.assertEqual(reading.voltage_mv, 12000)
        self.assertEqual(reading.power_mw, 24000)
        self.assertNotIn(
            "CURRENT_DERIVED_FROM_POWER_AND_VOLTAGE",
            reading.data_quality_flags,
        )

    def test_current_can_be_derived_from_power_and_voltage(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            label = root / "in1_label"
            voltage = root / "in1_input"
            power = root / "power1_input"
            label.write_text("VDD_IN\n", encoding="utf-8")
            voltage.write_text("12000\n", encoding="utf-8")
            power.write_text("24000000\n", encoding="utf-8")

            source = JetsonVddInSource(
                HwmonSensorFiles(None, voltage, power, label),
                MonitorConfig(),
            )
            reading = source.read()

        self.assertEqual(reading.current_ma, 2000)
        self.assertIn(
            "CURRENT_DERIVED_FROM_POWER_AND_VOLTAGE",
            reading.data_quality_flags,
        )

    def test_non_numeric_sensor_data_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            label = root / "in1_label"
            current = root / "curr1_input"
            label.write_text("VDD_IN\n", encoding="utf-8")
            current.write_text("not-a-number\n", encoding="utf-8")

            source = JetsonVddInSource(
                HwmonSensorFiles(current, None, None, label),
                MonitorConfig(),
            )
            with self.assertRaises(SensorDataError):
                source.read()


class AlwaysFailingSource:
    def read(self) -> SensorReading:
        raise OSError("simulated sensor failure")


class FixedSource:
    def __init__(self, current_ma: int = 2000) -> None:
        self.current_ma = current_ma

    def read(self) -> SensorReading:
        return SensorReading(
            current_ma=self.current_ma,
            voltage_mv=12000,
            power_mw=24000,
            sensor_source="test",
        )


class TestMonitorLoop(unittest.TestCase):
    def identity(self, config: MonitorConfig) -> MonitorIdentity:
        return MonitorIdentity(
            monitor_session_id="session-test",
            run_id="run-test",
            jetson_id="jetson-test",
            boot_id="boot-test",
            software_version="test",
            git_commit="deadbeef",
            configuration_fingerprint=config.fingerprint(),
        )

    def test_max_samples_counts_failed_attempts(self) -> None:
        config = MonitorConfig(sample_interval_seconds=0.0001)
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "power.jsonl"
            with redirect_stdout(io.StringIO()):
                result = run_monitor(
                    AlwaysFailingSource(),
                    config,
                    log_path,
                    identity=self.identity(config),
                    max_samples=3,
                )
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        self.assertEqual(result, 0)
        shutdown = records[-1]
        self.assertEqual(shutdown["event_type"], "POWER_MONITOR_STOPPED")
        self.assertEqual(shutdown["attempt_count"], 3)
        self.assertEqual(shutdown["successful_sample_count"], 0)
        self.assertTrue(
            any(r.get("event_type") == "POWER_SENSOR_ERROR" for r in records)
        )

    def test_records_have_strictly_increasing_sequences(self) -> None:
        config = MonitorConfig(sample_interval_seconds=0.0001)
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "power.jsonl"
            with redirect_stdout(io.StringIO()):
                run_monitor(
                    FixedSource(),
                    config,
                    log_path,
                    identity=self.identity(config),
                    max_samples=3,
                )
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]

        sequences = [record["sequence"] for record in records]
        self.assertEqual(sequences, list(range(1, len(sequences) + 1)))
        self.assertTrue(all(record["run_id"] == "run-test" for record in records))


if __name__ == "__main__":
    unittest.main()
