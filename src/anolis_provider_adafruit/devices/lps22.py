"""ST LPS22 on Adafruit's breakout (4633): pressure and temperature (STEMMA QT,
I2C 0x5D or 0x5C).

Driver: adafruit-circuitpython-lps2x.

A chip that restarted on its own (a brown-out) comes back in one-shot mode and
stops measuring, while its data registers still read, so the readings would
freeze with no error. The board reads the data rate back from the chip on every
read, through the driver, and sets the chip up again, keeping the rate, when it
finds one-shot mode. The rate is never set to one-shot here.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from anolis_provider_sdk import Arg, Category, DeviceType, Function, Signal, ValueType

from ..mock import FakeLPS22
from .base import DeviceDriver, NotAnswering, nothing_at

log = logging.getLogger(__name__)

_ONE_SHOT = 0
_RATES = {
    "1": "LPS22_RATE_1_HZ",
    "10": "LPS22_RATE_10_HZ",
    "25": "LPS22_RATE_25_HZ",
    "50": "LPS22_RATE_50_HZ",
    "75": "LPS22_RATE_75_HZ",
}

TYPE = DeviceType(
    id="lps22",
    signals=(
        Signal("pressure", ValueType.DOUBLE, unit="hPa", description="Barometric pressure"),
        Signal("temperature", ValueType.DOUBLE, unit="C", description="Sensor temperature"),
        Signal(
            "data_rate",
            ValueType.DOUBLE,
            unit="Hz",
            description="Rate the chip measures at, read back from the chip",
            default=False,
            poll_hint_hz=0.0,
        ),
    ),
    functions=(
        Function(
            "set_data_rate",
            description="Set the rate the chip measures at",
            args=(
                Arg(
                    "rate_hz",
                    ValueType.STRING,
                    allowed_values=tuple(_RATES),
                    description="Measurement rate in Hz",
                ),
            ),
            category=Category.CONFIG,
            is_idempotent=True,
        ),
    ),
)


class LPS22(DeviceDriver):
    type = TYPE
    addresses = (0x5D, 0x5C)

    def __init__(self, i2c: Any, address: int) -> None:
        self._i2c = i2c
        self._address = address
        self._rate: int | None = None  # None: the driver's default
        self._sensor = self._open()

    def _open(self) -> Any:
        import adafruit_lps2x

        # The driver's constructor checks the chip id, resets the chip and starts it measuring.
        try:
            sensor = adafruit_lps2x.LPS22(self._i2c, self._address)
        except ValueError as err:
            if not nothing_at(err):
                raise
            raise NotAnswering(f"nothing answers at {self._address:#04x}") from err
        except RuntimeError as err:
            if not str(err).startswith("Failed to find LPS2X"):
                raise
            raise NotAnswering(f"no LPS22 at {self._address:#04x}: {err}") from err
        if self._rate is not None:
            sensor.data_rate = self._rate
        return sensor

    @staticmethod
    def fake() -> FakeLPS22:
        return FakeLPS22()

    def read(self, signal_ids: list[str]) -> Mapping[str, Any]:
        import adafruit_lps2x

        if self._sensor.data_rate == _ONE_SHOT:
            log.warning("LPS22 at %#04x restarted; setting it up again", self._address)
            self._sensor = self._open()
        s = self._sensor
        getters = {
            "pressure": lambda: s.pressure,
            "temperature": lambda: s.temperature,
            "data_rate": lambda: adafruit_lps2x.Rate.string[s.data_rate],
        }
        return {sid: float(getters[sid]()) for sid in signal_ids}

    def call(self, function: Function, args: Mapping[str, Any]) -> Mapping[str, Any] | None:
        import adafruit_lps2x

        if function.name == "set_data_rate":
            self._rate = getattr(adafruit_lps2x.Rate, _RATES[args["rate_hz"]])
            self._sensor.data_rate = self._rate
        return None
