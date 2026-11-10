"""Sensirion SHT31-D on Adafruit's breakout (2857): temperature and humidity
(STEMMA QT, I2C 0x44 or 0x45).

Driver: adafruit-circuitpython-sht31d.

The driver's constructor only sends a break and a soft reset, so the board
reads the serial number, which comes back with a CRC, to check that an SHT3x
answers. A reading whose CRC fails raises RuntimeError in the driver; the board
reports it as a failed transfer, so the provider retries the board rather than
treating it as a provider fault.

Measurements are single shots at high repeatability, so each reading is a new
measurement and a chip that restarted needs no setting up. The heater is not
exposed, as on the SHT4x: it is left off.
"""

from __future__ import annotations

import errno
from collections.abc import Mapping
from typing import Any

from anolis_provider_sdk import DeviceType, Function, Signal, ValueType

from ..mock import FakeSHT3x
from .base import DeviceDriver, NotAnswering, nothing_at

TYPE = DeviceType(
    id="sht3x",
    signals=(
        Signal("temperature", ValueType.DOUBLE, unit="C", description="Air temperature"),
        Signal("relative_humidity", ValueType.DOUBLE, unit="%", description="Relative humidity"),
    ),
)


class SHT3x(DeviceDriver):
    type = TYPE
    addresses = (0x44, 0x45)

    def __init__(self, i2c: Any, address: int) -> None:
        import adafruit_sht31d

        try:
            self._sensor = adafruit_sht31d.SHT31D(i2c, address)
        except ValueError as err:
            if not nothing_at(err):
                raise
            raise NotAnswering(f"nothing answers at {address:#04x}") from err
        try:
            self._sensor.serial_number  # noqa: B018 - read for its CRC check
        except RuntimeError as err:
            if str(err) != "CRC mismatch":
                raise
            raise NotAnswering(f"no SHT3x at {address:#04x}: its serial number did not read back intact") from err

    @staticmethod
    def fake() -> FakeSHT3x:
        return FakeSHT3x()

    def read(self, signal_ids: list[str]) -> Mapping[str, Any]:
        getters = {
            "temperature": lambda: self._sensor.temperature,
            "relative_humidity": lambda: self._sensor.relative_humidity,
        }
        try:
            return {sid: float(getters[sid]()) for sid in signal_ids}
        except RuntimeError as err:  # the driver's CRC check
            if str(err) != "CRC mismatch":
                raise
            raise OSError(errno.EIO, f"SHT3x reading failed its CRC: {err}") from err

    def call(self, function: Function, args: Mapping[str, Any]) -> Mapping[str, Any] | None:
        raise NotImplementedError("the SHT3x declares no functions")
