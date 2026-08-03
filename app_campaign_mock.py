"""Preview the 2026 campaign GUI without contacting a Jetson."""

import tkinter as tk

from coordinator.campaign_ui import apply_campaign_ui
from coordinator.campaign_ui_v2 import apply_campaign_ui_v2
from coordinator.transport import MockTransport
from coordinator.ui import TestCoordinatorApp


def main() -> None:
    root = tk.Tk()
    app = TestCoordinatorApp(master=root, transport=MockTransport())
    apply_campaign_ui(app)
    apply_campaign_ui_v2(app)
    root.mainloop()


if __name__ == "__main__":
    main()
