"""Background UDP receiver and durable laptop-side power telemetry log."""

from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from power_telemetry_protocol import (
    DEFAULT_POWER_TELEMETRY_PORT,
    MAX_POWER_TELEMETRY_DATAGRAM_BYTES,
    PowerTelemetryModel,
    PowerTelemetryProtocolError,
    PowerTelemetrySnapshot,
    decode_power_telemetry_datagram,
)


def utc_timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class PowerTelemetryNotice:
    """Non-fatal receiver status delivered through the GUI queue."""

    level: str
    message: str


PowerTelemetryQueueItem = PowerTelemetrySnapshot | PowerTelemetryNotice


class ReceivedTelemetryLogger:
    """Append immutable receive envelopes to a laptop-side JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(
        self,
        record: dict[str, Any],
        sender: tuple[str, int],
    ) -> None:
        envelope = {
            "schema_version": 1,
            "record_type": "power_telemetry_received",
            "received_at_utc": utc_timestamp(),
            "sender_ip": sender[0],
            "sender_port": sender[1],
            "record": record,
        }
        serialized = json.dumps(
            envelope,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

        with self._lock:
            with self.path.open(
                "a",
                encoding="utf-8",
                newline="\n",
            ) as handle:
                handle.write(serialized)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())


class PowerTelemetryReceiver:
    """Receive, validate, persist, and enqueue power records off the GUI thread."""

    def __init__(
        self,
        *,
        bind_host: str = "0.0.0.0",
        port: int = DEFAULT_POWER_TELEMETRY_PORT,
        output_queue: queue.Queue[PowerTelemetryQueueItem],
        log_path: Path,
        socket_timeout_seconds: float = 0.5,
        max_datagram_bytes: int = MAX_POWER_TELEMETRY_DATAGRAM_BYTES,
    ) -> None:
        if not isinstance(bind_host, str):
            raise TypeError("bind_host must be a string")
        if isinstance(port, bool) or not isinstance(port, int):
            raise TypeError("port must be an integer")
        if not 0 <= port <= 65_535:
            raise ValueError("port must be from 0 to 65535")
        if socket_timeout_seconds <= 0:
            raise ValueError("socket_timeout_seconds must be positive")
        if max_datagram_bytes <= 0:
            raise ValueError("max_datagram_bytes must be positive")

        self.bind_host = bind_host
        self.port = port
        self.output_queue = output_queue
        self.log_path = log_path
        self.socket_timeout_seconds = socket_timeout_seconds
        self.max_datagram_bytes = max_datagram_bytes

        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._bound_port: int | None = None
        self._model = PowerTelemetryModel()
        self._logger = ReceivedTelemetryLogger(log_path)

    @property
    def bound_port(self) -> int | None:
        return self._bound_port

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, ready_timeout_seconds: float = 2.0) -> None:
        if self.is_running:
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="power-telemetry-receiver",
            daemon=True,
        )
        self._thread.start()
        self._ready_event.wait(timeout=ready_timeout_seconds)

    def stop(self, *, join_timeout_seconds: float = 2.0) -> None:
        self._stop_event.set()
        current_socket = self._socket
        if current_socket is not None:
            try:
                current_socket.close()
            except OSError:
                pass
        thread = self._thread
        if thread is not None:
            thread.join(timeout=join_timeout_seconds)
        self._thread = None
        self._socket = None

    def _put_latest(self, item: PowerTelemetryQueueItem) -> None:
        try:
            self.output_queue.put_nowait(item)
            return
        except queue.Full:
            pass

        try:
            self.output_queue.get_nowait()
        except queue.Empty:
            pass

        try:
            self.output_queue.put_nowait(item)
        except queue.Full:
            pass

    def _run(self) -> None:
        udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket = udp_socket
        try:
            udp_socket.settimeout(self.socket_timeout_seconds)
            udp_socket.bind((self.bind_host, self.port))
            self._bound_port = int(udp_socket.getsockname()[1])
            self._put_latest(
                PowerTelemetryNotice(
                    "info",
                    f"Power telemetry listening on "
                    f"{self.bind_host or '0.0.0.0'}:{self._bound_port}",
                )
            )
            self._ready_event.set()

            while not self._stop_event.is_set():
                try:
                    payload, sender = udp_socket.recvfrom(
                        self.max_datagram_bytes + 1
                    )
                except socket.timeout:
                    continue
                except OSError as error:
                    if self._stop_event.is_set():
                        break
                    self._put_latest(
                        PowerTelemetryNotice(
                            "error",
                            f"Power telemetry socket error: {error}",
                        )
                    )
                    break

                try:
                    record = decode_power_telemetry_datagram(
                        payload,
                        max_bytes=self.max_datagram_bytes,
                    )
                    self._logger.append(record, sender)
                    snapshot = self._model.apply(
                        record,
                        received_monotonic=time.monotonic(),
                    )
                except (
                    OSError,
                    TypeError,
                    PowerTelemetryProtocolError,
                ) as error:
                    self._put_latest(
                        PowerTelemetryNotice(
                            "warning",
                            f"Rejected power telemetry from "
                            f"{sender[0]}:{sender[1]}: {error}",
                        )
                    )
                    continue

                if snapshot is not None:
                    self._put_latest(snapshot)

        except OSError as error:
            self._put_latest(
                PowerTelemetryNotice(
                    "error",
                    f"Could not bind power telemetry receiver to "
                    f"{self.bind_host}:{self.port}: {error}",
                )
            )
            self._ready_event.set()
        finally:
            self._ready_event.set()
            try:
                udp_socket.close()
            except OSError:
                pass
            self._socket = None
