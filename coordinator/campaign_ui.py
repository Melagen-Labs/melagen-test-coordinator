"""Apply the 2026 campaign selection matrix to the existing Tk GUI."""

from __future__ import annotations

from types import MethodType
from typing import Any

from tkinter import ttk

from coordinator.campaign_config import (
    CAMPAIGN_BEAM_ENERGIES_MEV,
    CAMPAIGN_SHIELDING_MATERIALS,
    MLC1_REFERENCE_LEVELS_MM,
    format_campaign_summary,
    get_shield_configuration,
    reference_levels_for,
)


def _replace_label_text(app: Any, old: str, new: str) -> None:
    """Replace one top-level form label without rebuilding the existing GUI."""

    for child in app.winfo_children():
        if isinstance(child, ttk.Label) and child.cget("text") == old:
            child.configure(text=new)
            return


def apply_campaign_ui(app: Any) -> None:
    """Load campaign energies and automatic shield-equivalence display.

    Protocol version 1 still transmits the MLC1 reference level in
    ``shielding_thickness_mm``. This adapter adds the material-specific physical
    thickness and configuration ID to the operator summary without changing the
    wire format yet.
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

    _replace_label_text(
        app,
        "Shielding Thickness:",
        "MLC1 Reference Level:",
    )

    original_apply_control_state = app._apply_control_state

    def campaign_update_summary(self: Any) -> None:
        material = self.material_var.get()
        allowed = reference_levels_for(material)

        try:
            reference_mm = int(self.thickness_var.get())
        except ValueError:
            reference_mm = allowed[0]

        if reference_mm not in allowed:
            reference_mm = 0 if material == "Bare" else 12
            self.thickness_var.set(str(reference_mm))

        thickness_box.configure(values=[str(value) for value in allowed])

        configuration = get_shield_configuration(material, reference_mm)
        self.campaign_shield_configuration = configuration
        self.campaign_configuration_id = configuration.configuration_id
        self.campaign_actual_thickness_mm = configuration.actual_thickness_mm

        try:
            beam_energy = int(self.energy_var.get())
        except ValueError:
            beam_energy = CAMPAIGN_BEAM_ENERGIES_MEV[0]

        self.summary_var.set(
            format_campaign_summary(beam_energy, configuration)
        )

        if self.coordinator_state.value == "idle":
            thickness_box.configure(
                state="disabled" if material == "Bare" else "readonly"
            )

    def campaign_apply_control_state(self: Any) -> None:
        original_apply_control_state()
        if (
            self.coordinator_state.value == "idle"
            and self.material_var.get() == "Bare"
        ):
            thickness_box.configure(state="disabled")

    app._update_summary = MethodType(campaign_update_summary, app)
    app._apply_control_state = MethodType(campaign_apply_control_state, app)

    app.master.title("Jetson Proton Test Coordinator - 2026 Campaign")
    app._update_summary()
    app._apply_control_state()
    app._append_log(
        "2026 campaign matrix loaded: 50/63/125/200 MeV and automatic "
        "MLC1/MLC2/aluminium equivalent-thickness display."
    )
