"""Tests for the 2026 campaign shield-equivalence matrix."""

import unittest

from coordinator.campaign_config import (
    CAMPAIGN_BEAM_ENERGIES_MEV,
    format_campaign_summary,
    get_shield_configuration,
    reference_levels_for,
)


class TestCampaignConfig(unittest.TestCase):
    def test_campaign_energies(self) -> None:
        self.assertEqual(CAMPAIGN_BEAM_ENERGIES_MEV, (50, 63, 125, 200))

    def test_mlc2_12_equivalence(self) -> None:
        config = get_shield_configuration("MLC2", 12)
        self.assertEqual(config.actual_thickness_mm, 10.83)
        self.assertEqual(config.configuration_id, "M2-E12")

    def test_aluminium_16_equivalence(self) -> None:
        config = get_shield_configuration("Aluminium", 16)
        self.assertEqual(config.actual_thickness_mm, 7.71)
        self.assertEqual(config.configuration_id, "AL-E16")

    def test_bare_control(self) -> None:
        config = get_shield_configuration("Bare", 0)
        self.assertEqual(config.actual_thickness_mm, 0.0)
        self.assertEqual(config.configuration_id, "B00")
        self.assertEqual(reference_levels_for("Bare"), (0,))

    def test_summary_contains_actual_and_id(self) -> None:
        config = get_shield_configuration("MLC2", 8)
        summary = format_campaign_summary(125, config)
        self.assertIn("actual 7.22 mm", summary)
        self.assertIn("M2-E08", summary)

    def test_invalid_reference_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            get_shield_configuration("MLC1", 10)


if __name__ == "__main__":
    unittest.main()
