import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from rollout_control.contracts import RolloutState, RolloutWave, VehicleSnapshot


vehicle = VehicleSnapshot("VIN-8", "hw-b", "sw-1", 82)
wave = RolloutWave("W-2", "pkg-7", (vehicle,), RolloutState.PENDING)
print(json.dumps({"wave": wave.wave_id, "vehicles": len(wave.vehicles), "state": wave.state.value}, ensure_ascii=False))
