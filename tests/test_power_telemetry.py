from __future__ import annotations

import json
import queue
import socket
import tempfile
import time
import unittest
from pathlib import Path

from coordinator.power_telemetry import (
    PowerTelemetryNotice,
    PowerTelemetryReceiver,
)
from power_monitor import (
    MonitorConfig,
    SensorReading,
    run_monitor,
)
from power_telemetry_protocol import (
    PowerTelemetryModel,
    PowerTelemetryProtocolError,
    PowerTelemetrySnapshot,
    UdpTelemetryPublisher,
    decode_power_telemetry_datagram,
    encode_power_telemetry_record,
    validate_power_telemetry_record,
)


def make_record(
    *,
    sequence: int = 1,
    session: str = "session-1",
    record_type: str = "power_sample",
    event_type: str | None = None,
    state: str = "NORMAL",
    current_ma: int | None = 2000,
) -> dict[str, object]:
    record: dict[str, object] = {
        "schema_version": 1,
        "record_type": record_type,
        "event_type": event_type,
        "event_id": None if event_type is None else "event-1",
        "sequence": sequence,
        "recorded_at_utc": "2026-08-01T03:00:00.000Z",
        "monotonic_ns": sequence * 1_000_000,
        "monitor_session_id": session,
        "run_id": "run-1",
        "jetson_id": "jetson-1",
        "boot_id": "boot-1",
        "configuration_fingerprint": "fingerprint",
        "software_version": "0.3.0",
        "git_commit": "abcdef123456",
        "measurement_state": state,
        "red_flag_latched": False,
    }
    if current_ma is not None:
        record["current_ma"] = current_ma
        record["voltage_mv"] = 12000
        record["power_mw"] = int(current_ma * 12)
    return record


class TestPowerTelemetryProtocol(unittest.TestCase):
    def test_round_trip_preserves_record(self) -> None:
        record = make_record()
        payload = encode_power_telemetry_record(record)
        self.assertEqual(decode_power_telemetry_datagram(payload), record)

    def test_invalid_schema_is_rejected(self) -> None:
        record = make_record()
        record["schema_version"] = 2
        with self.assertRaises(PowerTelemetryProtocolError):
            validate_power_telemetry_record(record)

    def test_missing_session_is_rejected(self) -> None:
        record = make_record()
        del record["monitor_session_id"]
        with self.assertRaises(PowerTelemetryProtocolError):
            encode_power_telemetry_record(record)

    def test_non_object_json_is_rejected(self) -> None:
        with self.assertRaises(PowerTelemetryProtocolError):
            decode_power_telemetry_datagram(b"[]")

    def test_oversized_datagram_is_rejected(self) -> None:
        record = make_record()
        record["padding"] = "x" * 1000
        with self.assertRaises(PowerTelemetryProtocolError):
            encode_power_telemetry_record(record, max_bytes=100)


class TestPowerTelemetryModel(unittest.TestCase):
    def test_duplicate_and_out_of_order_records_are_ignored(self) -> None:
        model = PowerTelemetryModel()
        first = model.apply(make_record(sequence=2), received_monotonic=1.0)
        duplicate = model.apply(make_record(sequence=2), received_monotonic=2.0)
        older = model.apply(make_record(sequence=1), received_monotonic=3.0)
        self.assertIsNotNone(first)
        self.assertIsNone(duplicate)
        self.assertIsNone(older)

    def test_new_session_accepts_sequence_one(self) -> None:
        model = PowerTelemetryModel()
        model.apply(make_record(sequence=9, session="a"), received_monotonic=1.0)
        snapshot = model.apply(
            make_record(sequence=1, session="b"),
            received_monotonic=2.0,
        )
        self.assertIsNotNone(snapshot)
        assert snapshot is not None
        self.assertEqual(snapshot.monitor_session_id, "b")
        self.assertEqual(snapshot.sequence, 1)

    def test_delayed_packet_from_retired_session_is_ignored(self) -> None:
        model = PowerTelemetryModel()
        model.apply(make_record(sequence=9, session="old"), received_monotonic=1.0)
        model.apply(make_record(sequence=1, session="new"), received_monotonic=2.0)
        delayed = model.apply(
            make_record(sequence=10, session="old"),
            received_monotonic=3.0,
        )
        self.assertIsNone(delayed)
        assert model.latest is not None
        self.assertEqual(model.latest.monitor_session_id, "new")

    def test_event_without_measurement_keeps_last_sample_values(self) -> None:
        model = PowerTelemetryModel()
        model.apply(make_record(sequence=1), received_monotonic=1.0)
        event = make_record(
            sequence=2,
            record_type="power_state_event",
            event_type="POWER_STATE_CHANGED",
            state="OUT_OF_RANGE",
            current_ma=None,
        )
        snapshot = model.apply(event, received_monotonic=2.0)
        assert snapshot is not None
        self.assertEqual(snapshot.current_ma, 2000)
        self.assertEqual(snapshot.voltage_mv, 12000)
        self.assertEqual(snapshot.measurement_state, "OUT_OF_RANGE")


class TestUdpTelemetryPublisher(unittest.TestCase):
    def test_publisher_sends_one_valid_datagram(self) -> None:
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(1.0)
        port = int(receiver.getsockname()[1])

        publisher = UdpTelemetryPublisher("127.0.0.1", port)
        try:
            self.assertTrue(publisher.publish(make_record()))
            payload, _sender = receiver.recvfrom(60_001)
            decoded = decode_power_telemetry_datagram(payload)
            self.assertEqual(decoded["sequence"], 1)
            self.assertEqual(publisher.publish_count, 1)
            self.assertEqual(publisher.failure_count, 0)
        finally:
            publisher.close()
            receiver.close()


class TestPowerTelemetryReceiver(unittest.TestCase):
    def test_receiver_validates_logs_and_queues_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updates: queue.Queue[object] = queue.Queue(maxsize=16)
            log_path = Path(temp_dir) / "received.jsonl"
            receiver = PowerTelemetryReceiver(
                bind_host="127.0.0.1",
                port=0,
                output_queue=updates,
                log_path=log_path,
                socket_timeout_seconds=0.05,
            )
            receiver.start()
            self.assertIsNotNone(receiver.bound_port)

            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(
                    encode_power_telemetry_record(make_record()),
                    ("127.0.0.1", int(receiver.bound_port or 0)),
                )

                snapshot: PowerTelemetrySnapshot | None = None
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and snapshot is None:
                    try:
                        item = updates.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if isinstance(item, PowerTelemetrySnapshot):
                        snapshot = item

                self.assertIsNotNone(snapshot)
                assert snapshot is not None
                self.assertEqual(snapshot.current_ma, 2000)
                self.assertEqual(snapshot.measurement_state, "NORMAL")

                lines = log_path.read_text(encoding="utf-8").splitlines()
                self.assertEqual(len(lines), 1)
                envelope = json.loads(lines[0])
                self.assertEqual(
                    envelope["record"]["monitor_session_id"],
                    "session-1",
                )
            finally:
                sender.close()
                receiver.stop()

    def test_invalid_datagram_produces_notice_without_stopping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updates: queue.Queue[object] = queue.Queue(maxsize=16)
            receiver = PowerTelemetryReceiver(
                bind_host="127.0.0.1",
                port=0,
                output_queue=updates,
                log_path=Path(temp_dir) / "received.jsonl",
                socket_timeout_seconds=0.05,
            )
            receiver.start()
            sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sender.sendto(
                    b"not-json",
                    ("127.0.0.1", int(receiver.bound_port or 0)),
                )
                warning: PowerTelemetryNotice | None = None
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and warning is None:
                    try:
                        item = updates.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if (
                        isinstance(item, PowerTelemetryNotice)
                        and item.level == "warning"
                    ):
                        warning = item
                self.assertIsNotNone(warning)
                self.assertTrue(receiver.is_running)
            finally:
                sender.close()
                receiver.stop()


class StaticSource:
    def read(self) -> SensorReading:
        return SensorReading(
            current_ma=2000,
            voltage_mv=12000,
            power_mw=24000,
            sensor_source="test",
        )


class LogFirstPublisher:
    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self.sequences: list[int] = []

    def publish(self, record: dict[str, object]) -> bool:
        lines = self.log_path.read_text(encoding="utf-8").splitlines()
        self.asserted_record = json.loads(lines[-1])
        if self.asserted_record["sequence"] != record["sequence"]:
            raise AssertionError("record was published before it was logged")
        self.sequences.append(int(record["sequence"]))
        return True


class RaisingPublisher:
    def publish(self, _record: dict[str, object]) -> bool:
        raise ValueError("simulated telemetry failure")


class TestMonitorTelemetryIntegration(unittest.TestCase):
    def test_monitor_publishes_to_laptop_receiver(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            updates: queue.Queue[object] = queue.Queue(maxsize=32)
            receiver = PowerTelemetryReceiver(
                bind_host="127.0.0.1",
                port=0,
                output_queue=updates,
                log_path=Path(temp_dir) / "received.jsonl",
                socket_timeout_seconds=0.05,
            )
            receiver.start()
            publisher = UdpTelemetryPublisher(
                "127.0.0.1",
                int(receiver.bound_port or 0),
            )
            try:
                exit_code = run_monitor(
                    StaticSource(),
                    MonitorConfig(sample_interval_seconds=0.001),
                    Path(temp_dir) / "jetson.jsonl",
                    max_samples=1,
                    telemetry_publisher=publisher,
                )
                self.assertEqual(exit_code, 0)

                received_sample: PowerTelemetrySnapshot | None = None
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline and received_sample is None:
                    try:
                        item = updates.get(timeout=0.1)
                    except queue.Empty:
                        continue
                    if (
                        isinstance(item, PowerTelemetrySnapshot)
                        and item.current_ma == 2000
                    ):
                        received_sample = item
                self.assertIsNotNone(received_sample)
            finally:
                publisher.close()
                receiver.stop()

    def test_publish_failure_does_not_stop_local_monitoring(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "power.jsonl"
            config = MonitorConfig(sample_interval_seconds=0.001)
            exit_code = run_monitor(
                StaticSource(),
                config,
                log_path,
                max_samples=1,
                telemetry_publisher=RaisingPublisher(),
            )
            self.assertEqual(exit_code, 0)
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertGreaterEqual(len(records), 3)
            self.assertEqual(records[-1]["event_type"], "POWER_MONITOR_STOPPED")

    def test_monitor_persists_each_record_before_publishing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "power.jsonl"
            publisher = LogFirstPublisher(log_path)
            config = MonitorConfig(sample_interval_seconds=0.001)

            exit_code = run_monitor(
                StaticSource(),
                config,
                log_path,
                max_samples=1,
                telemetry_publisher=publisher,
            )

            self.assertEqual(exit_code, 0)
            logged = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                publisher.sequences,
                [record["sequence"] for record in logged],
            )


if __name__ == "__main__":
    unittest.main()
