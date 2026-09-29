import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from rollout_control.contracts import RolloutState, RolloutWave, VehicleSnapshot


class RolloutContractTests(unittest.TestCase):
    def test_wave_keeps_vehicle_snapshot(self):
        vehicle = VehicleSnapshot("V-1", "H-2", "S-3", 70)
        wave = RolloutWave("W-1", "P-1", (vehicle,), RolloutState.RUNNING)
        self.assertEqual(wave.vehicles[0].hardware_batch, "H-2")

    def test_invalid_battery_is_rejected(self):
        with self.assertRaises(ValueError):
            VehicleSnapshot("V-2", "H-2", "S-3", 101)


if __name__ == "__main__":
    unittest.main()
