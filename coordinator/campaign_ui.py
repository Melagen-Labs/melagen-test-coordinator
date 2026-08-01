"""Apply 2026 campaign controls to the existing Tk coordinator GUI."""

from __future__ import annotations

import csv
import time
import tkinter as tk
from types import MethodType
from typing import Any

from tkinter import messagebox, ttk

from coordinator.campaign_config import (
    CAMPAIGN_BEAM_ENERGIES_MEV,
    CAMPAIGN_DUT_TYPES,
    CAMPAIGN_SHIELDING_MATERIALS,
    CUSTOM_SHIELD_MATERIAL,
    DEFAULT_FLUX_P_CM2_S,
    MLC1_REFERENCE_LEVELS_MM,
    calculate_fluence,
    create_custom_shield_configuration,
    format_campaign_summary,
    format_scientific,
    get_shield_configuration,
    reference_levels_for,
)


FLUENCE_REFRESH_MS = 250


def _find_top_level_label(app: Any, text: str) -> ttk.Label | None:
    """Find one top-level form label by its current text."""

    for child in app.winfo_children():
        if isinstance(child, ttk.Label) and child.cget("text") == text:
            return child
    return None


def _shift_rows(app: Any, first_row: int, offset: int) -> None:
    """Move existing top-level grid children down to make room for new frames."""

    for child in app.winfo_children():
        grid_info = child.grid_info()
        if not grid_info:
            continue
        row = int(grid_info["row"])
        if row >= first_row:
            child.grid_configure(row=row + offset)


def apply_campaign_ui(app: Any) -> None:
    """Add campaign metadata, shield conversion, flux, and live fluence controls.

    The existing protocol-v1 request remains unchanged. Predefined shielding choices
    still transmit their existing reference level. DUT metadata, selected flux, live
    fluence, comments, and custom-shield details are recorded on the coordinator side.
    Custom shields are preview-only until the DUT wire protocol is extended.
    """

    energy_box, material_box, thickness_box = app._selection_widgets

    energy_box.configure(
        values=[str(value) for value in CAMPAIGN_BEAM_ENERGIES_MEV]
    )
    material_box.configure(values=CAMPAIGN_SHIELDING_MATERIALS)
    thickness_box.configure(
        values=[str(value) for value in MLC1_REFERENCE_LEVELS_MM]
    )

    if app.energy_var.get() not in {
        str(value) for value in CAMPAIGN_BEAM_ENERGIES_MEV
    }:
        app.energy_var.set("125")

    if app.material_var.get() not in CAMPAIGN_SHIELDING_MATERIALS:
        app.material_var.set("MLC1")

    if app.thickness_var.get() not in {
        str(value) for value in MLC1_REFERENCE_LEVELS_MM
    }:
        app.thickness_var.set("12")

    title_label = _find_top_level_label(
        app,
        "Jetson Proton Test Coordinator",
    )
    if title_label is not None:
        title_label.configure(text="Melagen Lab Test Coordinator")

    reference_label = _find_top_level_label(
        app,
        "Shielding Thickness:",
    )
    if reference_label is not None:
        reference_label.configure(text="MLC1 / MLC2 Reference Level:")

    _shift_rows(app, first_row=5, offset=2)
    app.rowconfigure(9, weight=0)
    app.rowconfigure(11, weight=1)
    app.rowconfigure(13, weight=1)

    app.dut_type_var = tk.StringVar(value=CAMPAIGN_DUT_TYPES[0])
    app.dut_serial_var = tk.StringVar()
    app.operator_comments_var = tk.StringVar()
    app.custom_shield_name_var = tk.StringVar()
    app.custom_shield_thickness_var = tk.StringVar()
    app.flux_exponent_var = tk.DoubleVar(value=7.0)
    app.flux_display_var = tk.StringVar()
    app.fluence_display_var = tk.StringVar(value="0.000e+00 p/cm²")

    app.campaign_accumulated_fluence = 0.0
    app.campaign_elapsed_seconds = 0.0
    app.campaign_selected_flux = DEFAULT_FLUX_P_CM2_S
    app._campaign_fluence_started_at = None
    app._campaign_fluence_after_id = None

    details_frame = ttk.LabelFrame(
        app,
        text="DUT and Run Information",
        padding=12,
    )
    details_frame.grid(
        row=5,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=(14, 8),
    )
    details_frame.columnconfigure(1, weight=1)
    details_frame.columnconfigure(3, weight=1)

    ttk.Label(details_frame, text="DUT:").grid(
        row=0, column=0, sticky="w", padx=(0, 8), pady=4
    )
    dut_box = ttk.Combobox(
        details_frame,
        textvariable=app.dut_type_var,
        values=CAMPAIGN_DUT_TYPES,
        state="readonly",
        width=23,
    )
    dut_box.grid(row=0, column=1, sticky="ew", padx=(0, 16), pady=4)

    ttk.Label(details_frame, text="DUT Serial Number:").grid(
        row=0, column=2, sticky="w", padx=(0, 8), pady=4
    )
    serial_entry = ttk.Entry(
        details_frame,
        textvariable=app.dut_serial_var,
        width=24,
    )
    serial_entry.grid(row=0, column=3, sticky="ew", pady=4)

    ttk.Label(details_frame, text="Comments / Run Notes:").grid(
        row=1, column=0, sticky="w", padx=(0, 8), pady=4
    )
    comments_entry = ttk.Entry(
        details_frame,
        textvariable=app.operator_comments_var,
    )
    comments_entry.grid(
        row=1, column=1, columnspan=3, sticky="ew", pady=4
    )

    custom_name_label = ttk.Label(details_frame, text="Custom Shield Name:")
    custom_name_entry = ttk.Entry(
        details_frame,
        textvariable=app.custom_shield_name_var,
    )
    custom_thickness_label = ttk.Label(
        details_frame,
        text="Custom Thickness (mm):",
    )
    custom_thickness_entry = ttk.Entry(
        details_frame,
        textvariable=app.custom_shield_thickness_var,
        width=14,
    )

    custom_name_label.grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
    custom_name_entry.grid(row=2, column=1, sticky="ew", padx=(0, 16), pady=4)
    custom_thickness_label.grid(row=2, column=2, sticky="w", padx=(0, 8), pady=4)
    custom_thickness_entry.grid(row=2, column=3, sticky="ew", pady=4)

    beam_frame = ttk.LabelFrame(
        app,
        text="Beam Flux and Calculated Fluence",
        padding=12,
    )
    beam_frame.grid(
        row=6,
        column=0,
        columnspan=2,
        sticky="ew",
        pady=(0, 8),
    )
    beam_frame.columnconfigure(1, weight=1)

    ttk.Label(beam_frame, text="Flux (p/cm²/s):").grid(
        row=0, column=0, sticky="w", padx=(0, 10)
    )
    flux_scale = tk.Scale(
        beam_frame,
        from_=3.0,
        to=8.0,
        resolution=0.1,
        orient="horizontal",
        showvalue=False,
        variable=app.flux_exponent_var,
        highlightthickness=0,
        length=360,
    )
    flux_scale.grid(row=0, column=1, sticky="ew")
    ttk.Label(
        beam_frame,
        textvariable=app.flux_display_var,
        width=22,
        anchor="e",
    ).grid(row=0, column=2, sticky="e", padx=(10, 0))

    ttk.Label(beam_frame, text="10³").grid(row=1, column=1, sticky="w")
    ttk.Label(beam_frame, text="10⁸").grid(row=1, column=1, sticky="e")

    ttk.Label(beam_frame, text="Accumulated Fluence:").grid(
        row=2, column=0, sticky="w", padx=(0, 10), pady=(8, 0)
    )
    ttk.Label(
        beam_frame,
        textvariable=app.fluence_display_var,
        font=("Segoe UI", 10, "bold"),
    ).grid(row=2, column=1, sticky="w", pady=(8, 0))
    ttk.Label(
        beam_frame,
        text="fluence = flux × active-test seconds",
    ).grid(row=2, column=2, sticky="e", padx=(10, 0), pady=(8, 0))

    original_apply_control_state = app._apply_control_state
    original_on_start_test = app._on_start_test
    original_on_stop_test = app._on_stop_test
    original_save_result_csv = app._save_result_csv

    def selected_flux() -> float:
        exponent = round(float(app.flux_exponent_var.get()), 1)
        return 10.0 ** exponent

    def update_flux_display(*_args: Any) -> None:
        flux = selected_flux()
        app.campaign_selected_flux = flux
        app.flux_display_var.set(f"{format_scientific(flux)} p/cm²/s")
        app._update_summary()

    def resolve_configuration() -> Any:
        material = app.material_var.get()
        if material == CUSTOM_SHIELD_MATERIAL:
            raw_thickness = app.custom_shield_thickness_var.get().strip()
            if not raw_thickness:
                raise ValueError("custom shield thickness is required")
            try:
                thickness = float(raw_thickness)
            except ValueError as error:
                raise ValueError(
                    "custom shield thickness must be numeric"
                ) from error
            return create_custom_shield_configuration(
                app.custom_shield_name_var.get(),
                thickness,
            )

        allowed = reference_levels_for(material)
        try:
            reference_mm = int(app.thickness_var.get())
        except ValueError:
            reference_mm = allowed[0]

        if reference_mm not in allowed:
            reference_mm = 0 if material == "Bare" else 12
            app.thickness_var.set(str(reference_mm))

        return get_shield_configuration(material, reference_mm)

    def campaign_update_summary(self: Any) -> None:
        material = self.material_var.get()
        is_custom = material == CUSTOM_SHIELD_MATERIAL

        if is_custom:
            custom_name_label.grid()
            custom_name_entry.grid()
            custom_thickness_label.grid()
            custom_thickness_entry.grid()
            thickness_box.configure(values=(), state="disabled")
            if reference_label is not None:
                reference_label.configure(text="MLC1 / MLC2 Reference Level:")
        else:
            custom_name_label.grid_remove()
            custom_name_entry.grid_remove()
            custom_thickness_label.grid_remove()
            custom_thickness_entry.grid_remove()

            allowed = reference_levels_for(material)
            thickness_box.configure(values=[str(value) for value in allowed])
            if reference_label is not None:
                if material in ("MLC1", "MLC2"):
                    reference_label.configure(
                        text="MLC1 / MLC2 Reference Level:"
                    )
                elif material == "Aluminium":
                    reference_label.configure(text="Aluminium Reference Level:")
                else:
                    reference_label.configure(text="Reference Level:")

        try:
            configuration = resolve_configuration()
            self.campaign_shield_configuration = configuration
            self.campaign_configuration_id = configuration.configuration_id
            self.campaign_actual_thickness_mm = configuration.actual_thickness_mm
            shield_summary = format_campaign_summary(
                int(self.energy_var.get()),
                configuration,
            )
            if is_custom:
                shield_summary += " | preview only"
        except (TypeError, ValueError):
            self.campaign_shield_configuration = None
            self.campaign_configuration_id = ""
            self.campaign_actual_thickness_mm = None
            shield_summary = (
                f"{self.energy_var.get()} MeV | Custom shield: "
                "enter name and thickness"
            )

        serial = self.dut_serial_var.get().strip() or "not entered"
        self.summary_var.set(
            f"{shield_summary}\n"
            f"DUT: {self.dut_type_var.get()} | Serial: {serial} | "
            f"Flux: {format_scientific(selected_flux())} p/cm²/s"
        )

        if self.coordinator_state.value == "idle":
            if material in ("Bare", CUSTOM_SHIELD_MATERIAL):
                thickness_box.configure(state="disabled")
            else:
                thickness_box.configure(state="readonly")

    def refresh_fluence() -> None:
        if app._campaign_fluence_started_at is None:
            return
        elapsed = max(0.0, time.monotonic() - app._campaign_fluence_started_at)
        app.campaign_elapsed_seconds = elapsed
        app.campaign_accumulated_fluence = calculate_fluence(
            app.campaign_selected_flux,
            elapsed,
        )
        app.fluence_display_var.set(
            f"{format_scientific(app.campaign_accumulated_fluence)} p/cm²"
        )

    def schedule_fluence_tick() -> None:
        refresh_fluence()
        if app.coordinator_state.value == "active":
            app._campaign_fluence_after_id = app.master.after(
                FLUENCE_REFRESH_MS,
                schedule_fluence_tick,
            )

    def cancel_fluence_tick() -> None:
        if app._campaign_fluence_after_id is not None:
            try:
                app.master.after_cancel(app._campaign_fluence_after_id)
            except tk.TclError:
                pass
            app._campaign_fluence_after_id = None

    def freeze_fluence() -> None:
        refresh_fluence()
        cancel_fluence_tick()
        app._campaign_fluence_started_at = None

    def campaign_apply_control_state(self: Any) -> None:
        original_apply_control_state()
        idle = self.coordinator_state.value == "idle"
        if idle and self._campaign_fluence_started_at is not None:
            freeze_fluence()

        dut_box.configure(state="readonly" if idle else "disabled")
        serial_entry.configure(state="normal" if idle else "disabled")
        comments_entry.configure(state="normal" if idle else "disabled")
        flux_scale.configure(state="normal" if idle else "disabled")

        custom_state = (
            "normal"
            if idle and self.material_var.get() == CUSTOM_SHIELD_MATERIAL
            else "disabled"
        )
        custom_name_entry.configure(state=custom_state)
        custom_thickness_entry.configure(state=custom_state)

        if idle:
            if self.material_var.get() in ("Bare", CUSTOM_SHIELD_MATERIAL):
                thickness_box.configure(state="disabled")
            else:
                thickness_box.configure(state="readonly")

    def campaign_on_start_test(self: Any) -> None:
        try:
            configuration = resolve_configuration()
        except (TypeError, ValueError) as error:
            messagebox.showerror(
                "Invalid Campaign Configuration",
                str(error),
                parent=self.master,
            )
            return

        if self.material_var.get() == CUSTOM_SHIELD_MATERIAL:
            messagebox.showinfo(
                "Custom Shield Preview",
                "The custom shield was captured in the GUI, but START_TEST is "
                "blocked until the Jetson request protocol supports custom material "
                "names and decimal thicknesses.",
                parent=self.master,
            )
            return

        self.campaign_selected_flux = selected_flux()
        self.campaign_run_metadata = {
            "dut_type": self.dut_type_var.get(),
            "dut_serial": self.dut_serial_var.get().strip(),
            "operator_comments": self.operator_comments_var.get().strip(),
            "flux_p_cm2_s": self.campaign_selected_flux,
            "fluence_formula": "flux_p_cm2_s * active_test_seconds",
            "shield_configuration_id": configuration.configuration_id,
            "shield_actual_thickness_mm": configuration.actual_thickness_mm,
        }

        original_on_start_test()

        if self.coordinator_state.value == "active":
            self.campaign_accumulated_fluence = 0.0
            self.campaign_elapsed_seconds = 0.0
            self.fluence_display_var.set("0.000e+00 p/cm²")
            self._campaign_fluence_started_at = time.monotonic()
            cancel_fluence_tick()
            schedule_fluence_tick()
            self._record_event(
                "CAMPAIGN_RUN_METADATA",
                **self.campaign_run_metadata,
            )

    def campaign_on_stop_test(
        self: Any,
        automatic: bool = False,
    ) -> None:
        refresh_fluence()
        original_on_stop_test(automatic=automatic)

        if self.coordinator_state.value == "idle":
            cancel_fluence_tick()
            self._record_event(
                "CAMPAIGN_FLUENCE_FINAL",
                flux_p_cm2_s=self.campaign_selected_flux,
                elapsed_active_seconds=self.campaign_elapsed_seconds,
                accumulated_fluence_p_cm2=self.campaign_accumulated_fluence,
            )

    def campaign_save_result_csv(
        self: Any,
        summary: dict[str, Any],
    ) -> Any:
        path = original_save_result_csv(summary)
        if path is None:
            return None

        metadata = getattr(self, "campaign_run_metadata", {})
        try:
            with open(path, "a", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow([])
                writer.writerow(["campaign_field", "value"])
                writer.writerow(["dut_type", metadata.get("dut_type", "")])
                writer.writerow(["dut_serial", metadata.get("dut_serial", "")])
                writer.writerow(
                    ["operator_comments", metadata.get("operator_comments", "")]
                )
                writer.writerow(
                    ["flux_p_cm2_s", metadata.get("flux_p_cm2_s", "")]
                )
                writer.writerow(
                    [
                        "accumulated_fluence_p_cm2",
                        self.campaign_accumulated_fluence,
                    ]
                )
                writer.writerow(
                    [
                        "shield_configuration_id",
                        metadata.get("shield_configuration_id", ""),
                    ]
                )
                writer.writerow(
                    [
                        "shield_actual_thickness_mm",
                        metadata.get("shield_actual_thickness_mm", ""),
                    ]
                )
        except OSError as error:
            self._append_log(f"Could not append campaign metadata to CSV: {error}")

        return path

    app._update_summary = MethodType(campaign_update_summary, app)
    app._apply_control_state = MethodType(campaign_apply_control_state, app)
    app._on_start_test = MethodType(campaign_on_start_test, app)
    app._on_stop_test = MethodType(campaign_on_stop_test, app)
    app._save_result_csv = MethodType(campaign_save_result_csv, app)

    app.start_button.configure(command=app._on_start_test)
    app.stop_button.configure(command=app._on_stop_test)

    app.flux_exponent_var.trace_add("write", update_flux_display)
    app.dut_type_var.trace_add("write", lambda *_args: app._update_summary())
    app.dut_serial_var.trace_add("write", lambda *_args: app._update_summary())
    app.custom_shield_name_var.trace_add(
        "write", lambda *_args: app._update_summary()
    )
    app.custom_shield_thickness_var.trace_add(
        "write", lambda *_args: app._update_summary()
    )

    app.master.title("Melagen Lab Test Coordinator")
    app.master.geometry("840x1040")
    app.master.minsize(760, 920)

    update_flux_display()
    app._update_summary()
    app._apply_control_state()
    app._append_log(
        "2026 campaign controls loaded: DUT metadata, custom-shield preview, "
        "10^3-10^8 flux slider, and live fluence calculation."
    )
