"""InvenSense MPU-6050 on Adafruit's breakout (3886): acceleration, rotation and
die temperature (STEMMA QT, I2C 0x68 or 0x69).

Driver: adafruit-circuitpython-mpu6050. Its constructor checks the chip id,
resets the chip and wakes it. It writes a gyroscope range of 500 degrees/s
before it wakes the chip, which ignores the write, so the board starts at the
chip's defaults: 2 g and 250 degrees/s. The driver scales readings by the range
it reads back, so they are correct either way.

A chip that restarted on its own (a brown-out) comes back asleep, and its data
registers stop changing while still reading, so the readings would freeze with
no error. The board reads the sleep bit back from the chip on every read,
through the driver, and sets the chip up again, keeping the ranges and the
filter, when it finds the chip asleep.

Connecting takes about half a second: the driver's constructor waits that long.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from anolis_provider_sdk import Arg, Category, DeviceType, Function, Signal, ValueType

from ..mock import FakeMPU6050
from .base import DeviceDriver, NotAnswering, nothing_at

log = logging.getLogger(__name__)

_ACCELERATION = ("acceleration_x", "acceleration_y", "acceleration_z")
_ROTATION = ("gyro_x", "gyro_y", "gyro_z")

# function -> (argument, driver attribute, driver class of constants, choice -> constant name)
_SETTINGS: dict[str, tuple[str, str, str, dict[str, str]]] = {
    "set_accelerometer_range": (
        "range_g",
        "accelerometer_range",
        "Range",
        {"2": "RANGE_2_G", "4": "RANGE_4_G", "8": "RANGE_8_G", "16": "RANGE_16_G"},
    ),
    "set_gyro_range": (
        "range_dps",
        "gyro_range",
        "GyroRange",
        {
            "250": "RANGE_250_DPS",
            "500": "RANGE_500_DPS",
            "1000": "RANGE_1000_DPS",
            "2000": "RANGE_2000_DPS",
        },
    ),
    "set_filter_bandwidth": (
        "bandwidth_hz",
        "filter_bandwidth",
        "Bandwidth",
        {
            "260": "BAND_260_HZ",
            "184": "BAND_184_HZ",
            "94": "BAND_94_HZ",
            "44": "BAND_44_HZ",
            "21": "BAND_21_HZ",
            "10": "BAND_10_HZ",
            "5": "BAND_5_HZ",
        },
    ),
}

_DESCRIPTIONS = {
    "set_accelerometer_range": "Set the accelerometer's full-scale range, in g",
    "set_gyro_range": "Set the gyroscope's full-scale range, in degrees/s",
    "set_filter_bandwidth": "Set the low-pass filter bandwidth, in Hz",
}

TYPE = DeviceType(
    id="mpu6050",
    signals=(
        *(
            Signal(sid, ValueType.DOUBLE, unit="m/s^2", description=f"Acceleration along {sid[-1].upper()}")
            for sid in _ACCELERATION
        ),
        *(
            Signal(sid, ValueType.DOUBLE, unit="rad/s", description=f"Rotation about {sid[-1].upper()}")
            for sid in _ROTATION
        ),
        Signal("temperature", ValueType.DOUBLE, unit="C", description="Die temperature"),
        *(
            Signal(
                attribute,
                ValueType.STRING,
                description=f"{_DESCRIPTIONS[name].removeprefix('Set the ')}, read back from the chip",
                default=False,
                poll_hint_hz=0.0,
            )
            for name, (_, attribute, _, _) in _SETTINGS.items()
        ),
    ),
    functions=tuple(
        Function(
            name,
            description=_DESCRIPTIONS[name],
            args=(Arg(arg, ValueType.STRING, allowed_values=tuple(choices), description=_DESCRIPTIONS[name]),),
            category=Category.CONFIG,
            is_idempotent=True,
        )
        for name, (arg, _, _, choices) in _SETTINGS.items()
    ),
)


class MPU6050(DeviceDriver):
    type = TYPE
    addresses = (0x68, 0x69)

    def __init__(self, i2c: Any, address: int) -> None:
        self._i2c = i2c
        self._address = address
        self._settings: dict[str, int] = {}  # driver attribute -> constant, those set by a call
        self._sensor = self._open()

    def _open(self) -> Any:
        import adafruit_mpu6050

        try:
            sensor = adafruit_mpu6050.MPU6050(self._i2c, self._address)
        except ValueError as err:
            if not nothing_at(err):
                raise
            raise NotAnswering(f"nothing answers at {self._address:#04x}") from err
        except RuntimeError as err:
            if not str(err).startswith("Failed to find MPU6050"):
                raise
            raise NotAnswering(f"no MPU-6050 at {self._address:#04x}: {err}") from err
        for attribute, value in self._settings.items():
            setattr(sensor, attribute, value)
        return sensor

    @staticmethod
    def fake() -> FakeMPU6050:
        return FakeMPU6050()

    def read(self, signal_ids: list[str]) -> Mapping[str, Any]:
        import adafruit_mpu6050

        if self._sensor.sleep:
            log.warning("MPU-6050 at %#04x restarted; setting it up again", self._address)
            self._sensor = self._open()
        s = self._sensor
        wanted = set(signal_ids)
        values: dict[str, Any] = {}
        if wanted.intersection(_ACCELERATION):
            values.update(zip(_ACCELERATION, map(float, s.acceleration), strict=True))
        if wanted.intersection(_ROTATION):
            values.update(zip(_ROTATION, map(float, s.gyro), strict=True))
        if "temperature" in wanted:
            values["temperature"] = float(s.temperature)
        for _, attribute, constants, choices in _SETTINGS.values():
            if attribute in wanted:
                cls = getattr(adafruit_mpu6050, constants)
                by_value = {getattr(cls, name): choice for choice, name in choices.items()}
                values[attribute] = by_value[getattr(s, attribute)]
        return {sid: values[sid] for sid in signal_ids}

    def call(self, function: Function, args: Mapping[str, Any]) -> Mapping[str, Any] | None:
        import adafruit_mpu6050

        arg, attribute, constants, choices = _SETTINGS[function.name]
        value = getattr(getattr(adafruit_mpu6050, constants), choices[args[arg]])
        setattr(self._sensor, attribute, value)
        self._settings[attribute] = value
        return None
