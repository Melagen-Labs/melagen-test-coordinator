"""Preview the 2026 campaign GUI without contacting a Jetson."""

import tkinter as tk

from coordinator.campaign_ui import apply_campaign_ui
from coordinator.transport import MockTransport
from coordinator.ui import TestCoordinatorApp


def main() -> None:
    root = tk.Tk()
    app = TestCoordinatorApp(master=root, transport=MockTransport())
    apply_campaign_ui(app)
    root.mainloop()


if __name__ == "__main__":
    main()
