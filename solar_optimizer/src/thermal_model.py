"""Simple thermal models for DHW tank and room (per AC unit)."""
from dataclasses import dataclass
from typing import Optional

import numpy as np

WATER_SPECIFIC_HEAT_KJ = 4.186  # kJ / (kg·K)
SLOT_HOURS = 0.5


@dataclass
class DHWModel:
    tank_liters: float
    loss_rate_c_per_hour: float
    cop: float
    comfort_min: float = 45.0
    max_temp: float = 58.0

    @property
    def thermal_mass_kwh_per_c(self) -> float:
        return self.tank_liters * WATER_SPECIFIC_HEAT_KJ / 3600

    def next_temp(self, current_temp: float, heat_energy_kwh: float) -> float:
        delta_heat = heat_energy_kwh / self.thermal_mass_kwh_per_c
        delta_loss = self.loss_rate_c_per_hour * SLOT_HOURS
        return current_temp + delta_heat - delta_loss

    def max_heat_per_slot_kwh(self, current_temp: float) -> float:
        headroom = max(0.0, self.max_temp - current_temp)
        return headroom * self.thermal_mass_kwh_per_c

    def electrical_power_w(self, heat_energy_kwh: float) -> float:
        if self.cop <= 0:
            return 0.0
        return heat_energy_kwh / self.cop / SLOT_HOURS * 1000


@dataclass
class ACRoomModel:
    beta: float = 0.05
    ac_efficiency: float = 3.0

    def next_temp(
        self,
        room_temp: float,
        outdoor_temp: float,
        ac_power_w: float,
        heating: bool = False,
    ) -> float:
        thermal_leakage = (outdoor_temp - room_temp) * self.beta
        ac_delta = (ac_power_w / 1000) * self.ac_efficiency * SLOT_HOURS
        ac_delta = ac_delta if heating else -ac_delta
        return room_temp + thermal_leakage + ac_delta

    @staticmethod
    def estimate_power_w(setpoint_delta: float, outdoor_temp: float, base_power_w: float = 800.0) -> float:
        return base_power_w + abs(setpoint_delta) * 150.0


DEFAULT_DHW_MODEL = DHWModel(
    tank_liters=200,
    loss_rate_c_per_hour=0.8,
    cop=3.0,
)
