"""Unit tests for power_monitor.py."""

from __future__ import annotations

import unittest

from power_monitor import (
    CurrentSpikeDetector,
    MonitorConfig,
)


class TestCurrentSpikeDetector(unittest.TestCase):
    def setUp(self) -> None:
        self.config = MonitorConfig(
            nominal_current_a=2.0,
            nominal_tolerance_a=0.1,
            trip_threshold_a=2.3,
            trip_duration_seconds=3.0,
            sample_interval_seconds=0.2,
        )
        self.detector = CurrentSpikeDetector(
            self.config
        )

    def test_nominal_band_is_normal(self) -> None:
        self.assertEqual(
            self.detector.update(1.9, 0.0).status,
            "NORMAL",
        )
        self.assertEqual(
            self.detector.update(2.0, 1.0).status,
            "NORMAL",
        )
        self.assertEqual(
            self.detector.update(2.1, 2.0).status,
            "NORMAL",
        )

    def test_2_2_a_is_out_of_range_not_red(self) -> None:
        result = self.detector.update(2.2, 0.0)
        self.assertEqual(result.status, "OUT_OF_RANGE")
        self.assertIsNone(result.event)

    def test_2_3_a_for_three_seconds_is_red(self) -> None:
        entered = self.detector.update(2.3, 10.0)
        self.assertEqual(entered.status, "TRIP_PENDING")
        self.assertEqual(
            entered.event,
            "CURRENT_THRESHOLD_ENTERED",
        )

        pending = self.detector.update(2.35, 12.9)
        self.assertEqual(pending.status, "TRIP_PENDING")

        red = self.detector.update(2.35, 13.0)
        self.assertEqual(red.status, "RED_FLAG")
        self.assertEqual(
            red.event,
            "CURRENT_RED_FLAG",
        )

    def test_drop_below_threshold_resets_timer(self) -> None:
        self.detector.update(2.35, 0.0)
        cleared = self.detector.update(2.2, 2.0)

        self.assertEqual(cleared.status, "OUT_OF_RANGE")
        self.assertEqual(
            cleared.event,
            "CURRENT_THRESHOLD_CLEARED",
        )

        restarted = self.detector.update(2.35, 2.1)
        self.assertEqual(
            restarted.status,
            "TRIP_PENDING",
        )


if __name__ == "__main__":
    unittest.main()
