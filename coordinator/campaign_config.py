"""2026 proton-campaign beam, DUT, flux, and shielding configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass


CAMPAIGN_BEAM_ENERGIES_MEV = (50, 63, 125, 200)
CAMPAIGN_DUT_TYPES = ("Jetson Orin Nano",)
CUSTOM_SHIELD_MATERIAL = "Custom"
CAMPAIGN_SHIELDING_MATERIALS = (
    "Bare",
    "MLC1",
    "MLC2",
    "Aluminium",
    CUSTOM_SHIELD_MATERIAL,
)
MLC1_REFERENCE_LEVELS_MM = (8, 12, 16)
MIN_FLUX_P_CM2_S = 1.0e3
MAX_FLUX_P_CM2_S = 1.0e8
DEFAULT_FLUX_P_CM2_S = 1.0e7


@dataclass(frozen=True)
class ShieldConfiguration:
    """One operator-selectable campaign shielding configuration."""

    material: str
    reference_mm: int
    actual_thickness_mm: float
    configuration_id: str


SHIELD_CONFIGURATIONS: dict[str, dict[int, ShieldConfiguration]] = {
    "Bare": {
        0: ShieldConfiguration("Bare", 0, 0.0, "B00"),
    },
    "MLC1": {
        8: ShieldConfiguration("MLC1", 8, 8.00, "M1-08"),
        12: ShieldConfiguration("MLC1", 12, 12.00, "M1-12"),
        16: ShieldConfiguration("MLC1", 16, 16.00, "M1-16"),
    },
    "MLC2": {
        8: ShieldConfiguration("MLC2", 8, 7.22, "M2-E08"),
        12: ShieldConfiguration("MLC2", 12, 10.83, "M2-E12"),
        16: ShieldConfiguration("MLC2", 16, 14.44, "M2-E16"),
    },
    "Aluminium": {
        8: ShieldConfiguration("Aluminium", 8, 3.85, "AL-E08"),
        12: ShieldConfiguration("Aluminium", 12, 5.78, "AL-E12"),
        16: ShieldConfiguration("Aluminium", 16, 7.71, "AL-E16"),
    },
}


def reference_levels_for(material: str) -> tuple[int, ...]:
    """Return valid MLC reference selections for one predefined material."""

    if material == CUSTOM_SHIELD_MATERIAL:
        return ()

    try:
        return tuple(SHIELD_CONFIGURATIONS[material])
    except KeyError as error:
        raise ValueError(f"Unsupported shielding material: {material}") from error


def get_shield_configuration(
    material: str,
    reference_mm: int,
) -> ShieldConfiguration:
    """Resolve a predefined material/reference choice to the physical coupon."""

    if not isinstance(material, str):
        raise TypeError("material must be a string")

    if type(reference_mm) is not int:
        raise TypeError("reference_mm must be an integer")

    if material == CUSTOM_SHIELD_MATERIAL:
        raise ValueError("Custom shields require a name and physical thickness")

    try:
        return SHIELD_CONFIGURATIONS[material][reference_mm]
    except KeyError as error:
        allowed = reference_levels_for(material)
        raise ValueError(
            f"Unsupported reference level {reference_mm} for {material}. "
            f"Allowed values: {allowed}"
        ) from error


def create_custom_shield_configuration(
    name: str,
    thickness_mm: float,
) -> ShieldConfiguration:
    """Validate operator-entered custom shield details for GUI metadata."""

    if not isinstance(name, str):
        raise TypeError("custom shield name must be a string")

    normalized_name = name.strip()
    if not normalized_name:
        raise ValueError("custom shield name is required")

    if isinstance(thickness_mm, bool) or not isinstance(thickness_mm, (int, float)):
        raise TypeError("custom shield thickness must be numeric")

    numeric_thickness = float(thickness_mm)
    if not math.isfinite(numeric_thickness) or numeric_thickness <= 0:
        raise ValueError("custom shield thickness must be greater than 0")

    return ShieldConfiguration(
        material=normalized_name,
        reference_mm=0,
        actual_thickness_mm=numeric_thickness,
        configuration_id="CUSTOM",
    )


def calculate_fluence(
    flux_p_cm2_s: float,
    elapsed_seconds: float,
) -> float:
    """Calculate accumulated fluence as flux multiplied by elapsed time."""

    for value, field_name in (
        (flux_p_cm2_s, "flux_p_cm2_s"),
        (elapsed_seconds, "elapsed_seconds"),
    ):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{field_name} must be numeric")
        if not math.isfinite(float(value)):
            raise ValueError(f"{field_name} must be finite")

    if flux_p_cm2_s < 0:
        raise ValueError("flux_p_cm2_s must not be negative")
    if elapsed_seconds < 0:
        raise ValueError("elapsed_seconds must not be negative")

    return float(flux_p_cm2_s) * float(elapsed_seconds)


def format_scientific(value: float) -> str:
    """Format a run quantity using compact scientific notation."""

    return f"{float(value):.3e}"


def format_campaign_summary(
    beam_energy_mev: int,
    configuration: ShieldConfiguration,
) -> str:
    """Build the compact operator-facing configuration summary."""

    if configuration.material == "Bare":
        return f"{beam_energy_mev} MeV | Bare control | {configuration.configuration_id}"

    if configuration.configuration_id == "CUSTOM":
        return (
            f"{beam_energy_mev} MeV | Custom: {configuration.material} | "
            f"actual {configuration.actual_thickness_mm:.2f} mm | CUSTOM"
        )

    return (
        f"{beam_energy_mev} MeV | {configuration.material} | "
        f"ref {configuration.reference_mm} mm -> "
        f"actual {configuration.actual_thickness_mm:.2f} mm | "
        f"{configuration.configuration_id}"
    )
