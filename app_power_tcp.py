"""Launch the real TCP coordinator with read-only UDP power telemetry."""

from __future__ import annotations

import argparse
import tkinter as tk

from coordinator.power_ui import PowerAwareCoordinatorApp
from coordinator.transport import TcpTransport
from power_telemetry_protocol import DEFAULT_POWER_TELEMETRY_PORT


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Launch the Jetson Proton Test Coordinator with live power "
            "telemetry."
        )
    )
    parser.add_argument(
        "--host",
        default="192.168.1.20",
        help="DUT control address (default: 192.168.1.20).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=6000,
        help="DUT control TCP port (default: 6000).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="Per-command timeout in seconds (default: 5.0).",
    )
    parser.add_argument(
        "--telemetry-bind-host",
        default="0.0.0.0",
        help="Laptop interface for UDP telemetry (default: all interfaces).",
    )
    parser.add_argument(
        "--telemetry-port",
        type=int,
        default=DEFAULT_POWER_TELEMETRY_PORT,
        help=(
            "Laptop UDP power telemetry port "
            f"(default: {DEFAULT_POWER_TELEMETRY_PORT})."
        ),
    )
    args = parser.parse_args()

    root = tk.Tk()
    transport = TcpTransport(
        host=args.host,
        port=args.port,
        timeout_seconds=args.timeout,
    )
    PowerAwareCoordinatorApp(
        master=root,
        transport=transport,
        telemetry_bind_host=args.telemetry_bind_host,
        telemetry_port=args.telemetry_port,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
