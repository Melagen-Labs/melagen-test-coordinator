"""2026 proton-campaign beam and shielding configuration matrix."""

from __future__ import annotations

from dataclasses import dataclass


CAMPAIGN_BEAM_ENERGIES_MEV = (50, 63, 125, 200)
CAMPAIGN_SHIELDING_MATERIALS = (
    "Bare",
    "MLC1",
    "MLC2",
    "Aluminium",
)
MLC1_REFERENCE_LEVELS_MM = (8, 12, 16)


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
    """Return valid MLC1-reference selections for one material."""

    try:
        return tuple(SHIELD_CONFIGURATIONS[material])
    except KeyError as error:
        raise ValueError(f"Unsupported shielding material: {material}") from error


def get_shield_configuration(
    material: str,
    reference_mm: int,
) -> ShieldConfiguration:
    """Resolve an operator material/reference choice to the physical coupon."""

    if not isinstance(material, str):
        raise TypeError("material must be a string")

    if type(reference_mm) is not int:
        raise TypeError("reference_mm must be an integer")

    try:
        return SHIELD_CONFIGURATIONS[material][reference_mm]
    except KeyError as error:
        allowed = reference_levels_for(material)
        raise ValueError(
            f"Unsupported reference level {reference_mm} for {material}. "
            f"Allowed values: {allowed}"
        ) from error


def format_campaign_summary(
    beam_energy_mev: int,
    configuration: ShieldConfiguration,
) -> str:
    """Build the compact operator-facing configuration summary."""

    if configuration.material == "Bare":
        return f"{beam_energy_mev} MeV | Bare control | {configuration.configuration_id}"

    return (
        f"{beam_energy_mev} MeV | {configuration.material} | "
        f"ref {configuration.reference_mm} mm -> "
        f"actual {configuration.actual_thickness_mm:.2f} mm | "
        f"{configuration.configuration_id}"
    )
