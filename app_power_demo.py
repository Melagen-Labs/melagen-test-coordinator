"""Launch the mock coordinator with live UDP power telemetry for bench testing."""

from __future__ import annotations

import argparse
import tkinter as tk

from coordinator.power_ui import PowerAwareCoordinatorApp
from power_telemetry_protocol import DEFAULT_POWER_TELEMETRY_PORT


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Launch the mock Test Coordinator with live power telemetry."
        )
    )
    parser.add_argument(
        "--telemetry-bind-host",
        default="127.0.0.1",
        help="Interface for local UDP telemetry (default: 127.0.0.1).",
    )
    parser.add_argument(
        "--telemetry-port",
        type=int,
        default=DEFAULT_POWER_TELEMETRY_PORT,
        help=(
            "UDP power telemetry port "
            f"(default: {DEFAULT_POWER_TELEMETRY_PORT})."
        ),
    )
    args = parser.parse_args()

    root = tk.Tk()
    PowerAwareCoordinatorApp(
        master=root,
        telemetry_bind_host=args.telemetry_bind_host,
        telemetry_port=args.telemetry_port,
    )
    root.mainloop()


if __name__ == "__main__":
    main()
