"""Power-aware coordinator UI using a thread-safe telemetry queue."""

from __future__ import annotations

import queue
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from coordinator.event_logger import EventLogger
from coordinator.power_telemetry import (
    PowerTelemetryNotice,
    PowerTelemetryQueueItem,
    PowerTelemetryReceiver,
)
from coordinator.transport import Transport
from coordinator.ui import TestCoordinatorApp
from power_telemetry_protocol import (
    DEFAULT_POWER_TELEMETRY_PORT,
    PowerTelemetrySnapshot,
)

POWER_UI_POLL_MS = 100
POWER_TELEMETRY_LIVE_SECONDS = 1.0
POWER_TELEMETRY_LOST_SECONDS = 3.0


def format_engineering_value(
    value: int | None,
    divisor: float,
    unit: str,
) -> str:
    if value is None:
        return "—"
    return f"{value / divisor:.3f} {unit}"


class PowerAwareCoordinatorApp(TestCoordinatorApp):
    """Existing coordinator plus a read-only live power status panel."""

    def __init__(
        self,
        master: tk.Tk,
        transport: Transport | None = None,
        event_logger: EventLogger | None = None,
        *,
        telemetry_bind_host: str = "0.0.0.0",
        telemetry_port: int = DEFAULT_POWER_TELEMETRY_PORT,
        telemetry_log_path: Path | None = None,
    ) -> None:
        super().__init__(
            master=master,
            transport=transport,
            event_logger=event_logger,
        )

        project_root = Path(__file__).resolve().parent.parent
        self._power_queue: queue.Queue[PowerTelemetryQueueItem] = (
            queue.Queue(maxsize=256)
        )
        self._latest_power_snapshot: PowerTelemetrySnapshot | None = None
        self._power_poll_after_id: str | None = None
        self._closing = False

        self.power_current_var = tk.StringVar(value="—")
        self.power_voltage_var = tk.StringVar(value="—")
        self.power_wattage_var = tk.StringVar(value="—")
        self.power_state_var = tk.StringVar(value="WAITING")
        self.power_latch_var = tk.StringVar(value="No")
        self.power_source_var = tk.StringVar(value="No telemetry received")
        self.power_link_var = tk.StringVar(value="LISTENING")

        self._insert_power_panel()
        self.master.geometry("820x760")
        self.master.minsize(720, 680)

        self.power_receiver = PowerTelemetryReceiver(
            bind_host=telemetry_bind_host,
            port=telemetry_port,
            output_queue=self._power_queue,
            log_path=(
                telemetry_log_path
                or project_root
                / "logs"
                / "power_telemetry_received.jsonl"
            ),
        )
        self.power_receiver.start()

        self.master.protocol("WM_DELETE_WINDOW", self._on_close)
        self._schedule_power_poll()

    def _insert_power_panel(self) -> None:
        """Insert a panel above the existing activity log."""
        for child in self.grid_slaves():
            grid_info = child.grid_info()
            row = int(grid_info.get("row", 0))
            if row >= 7:
                child.grid_configure(row=row + 1)

        self.rowconfigure(8, weight=0)
        self.rowconfigure(9, weight=1)

        frame = ttk.LabelFrame(
            self,
            text="Live Jetson Power Telemetry (read-only)",
            padding=10,
        )
        frame.grid(
            row=7,
            column=0,
            columnspan=2,
            sticky="ew",
            pady=(0, 12),
        )
        for column in range(6):
            frame.columnconfigure(column, weight=1)

        fields = (
            ("Current", self.power_current_var),
            ("Voltage", self.power_voltage_var),
            ("Power", self.power_wattage_var),
            ("State", self.power_state_var),
            ("Red flag", self.power_latch_var),
            ("Link", self.power_link_var),
        )
        for column, (label, variable) in enumerate(fields):
            ttk.Label(
                frame,
                text=label,
                font=("Segoe UI", 9, "bold"),
            ).grid(row=0, column=column, sticky="w", padx=4)
            ttk.Label(
                frame,
                textvariable=variable,
            ).grid(row=1, column=column, sticky="w", padx=4)

        ttk.Label(
            frame,
            textvariable=self.power_source_var,
        ).grid(
            row=2,
            column=0,
            columnspan=6,
            sticky="w",
            padx=4,
            pady=(6, 0),
        )

    def _schedule_power_poll(self) -> None:
        if self._closing:
            return
        self._power_poll_after_id = self.master.after(
            POWER_UI_POLL_MS,
            self._poll_power_queue,
        )

    def _poll_power_queue(self) -> None:
        self._power_poll_after_id = None
        while True:
            try:
                item = self._power_queue.get_nowait()
            except queue.Empty:
                break

            if isinstance(item, PowerTelemetrySnapshot):
                self._latest_power_snapshot = item
                self._apply_power_snapshot(item)
            elif isinstance(item, PowerTelemetryNotice):
                self._append_log(item.message)
                if item.level == "error":
                    self.power_link_var.set("ERROR")

        self._refresh_power_link_age()
        self._schedule_power_poll()

    def _apply_power_snapshot(
        self,
        snapshot: PowerTelemetrySnapshot,
    ) -> None:
        self.power_current_var.set(
            format_engineering_value(snapshot.current_ma, 1000.0, "A")
        )
        self.power_voltage_var.set(
            format_engineering_value(snapshot.voltage_mv, 1000.0, "V")
        )
        self.power_wattage_var.set(
            format_engineering_value(snapshot.power_mw, 1000.0, "W")
        )
        self.power_state_var.set(snapshot.measurement_state)
        self.power_latch_var.set(
            "YES — incident latched"
            if snapshot.red_flag_latched
            else "No"
        )
        self.power_source_var.set(
            f"Jetson: {snapshot.jetson_id} | "
            f"Run: {snapshot.run_id} | "
            f"Sequence: {snapshot.sequence}"
        )

    def _refresh_power_link_age(self) -> None:
        snapshot = self._latest_power_snapshot
        if snapshot is None:
            return

        age = max(0.0, time.monotonic() - snapshot.received_monotonic)
        if age <= POWER_TELEMETRY_LIVE_SECONDS:
            state = "LIVE"
        elif age <= POWER_TELEMETRY_LOST_SECONDS:
            state = f"STALE {age:.1f}s"
        else:
            state = f"LOST {age:.1f}s"
        self.power_link_var.set(state)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._power_poll_after_id is not None:
            try:
                self.master.after_cancel(self._power_poll_after_id)
            except tk.TclError:
                pass
            self._power_poll_after_id = None
        self.power_receiver.stop()
        self.master.destroy()
