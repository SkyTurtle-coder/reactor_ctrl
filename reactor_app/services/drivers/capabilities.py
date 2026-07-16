"""Device capability constants.

A capability is a plain string that describes what a device *can do*.
Drivers advertise their capabilities via ``DeviceDriver.get_capabilities()``.

The runtime can query capabilities to make decisions without hard-coding
protocol or ``device_type`` checks.  For example:

    if DeviceCapability.CAN_SET_TEMPERATURE in driver.get_capabilities():
        ...

New capabilities should be added here (and only here) so there is a single
source of truth for capability strings.
"""
from __future__ import annotations


class DeviceCapability:
    # Thermal control
    CAN_HEAT = "can_heat"
    CAN_COOL = "can_cool"
    CAN_SET_TEMPERATURE = "can_set_temperature"
    CAN_MEASURE_TEMPERATURE = "can_measure_temperature"

    # Mechanical
    CAN_STIR = "can_stir"
    CAN_PUMP = "can_pump"

    # Safety
    CAN_EMERGENCY_STOP = "can_emergency_stop"

    # Feedback / observability
    HAS_FEEDBACK = "has_feedback"
    CAN_WEIGH = "can_weigh"

    # Recipe / profile features
    SUPPORTS_RAMP = "supports_ramp"
    SUPPORTS_MANUAL_MODE = "supports_manual_mode"
    SUPPORTS_RECIPE_MODE = "supports_recipe_mode"
