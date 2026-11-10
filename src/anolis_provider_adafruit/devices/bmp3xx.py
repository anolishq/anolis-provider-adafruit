"""Bosch BMP388 and BMP390 on Adafruit boards (BMP388 3966, BMP390 4816):
pressure and temperature (STEMMA QT, I2C 0x77 or 0x76).

Driver: adafruit-circuitpython-bmp3xx. Its constructor checks the chip id and
resets the chip.

Each reading triggers its own forced-mode measurement, so a chip that restarted
keeps giving correct readings. It does lose the oversampling and filter
settings, and the board does not notice between polls; the read-back signals
show them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from anolis_provider_sdk import Arg, Category, DeviceType, Function, Signal, ValueType

from ..mock import FakeBMP3xx
from .base import DeviceDriver, NotAnswering, nothing_at

_OVERSAMPLING = ("1", "2", "4", "8", "16", "32")
_FILTER = ("0", "2", "4", "8", "16", "32", "64", "128")


def _setting(name: str, arg: str, allowed: tuple[str, ...], description: str) -> Function:
    return Function(
        name,
        description=description,
        args=(Arg(arg, ValueType.STRING, allowed_values=allowed, description=description),),
        category=Category.CONFIG,
        is_idempotent=True,
    )


def _read_back(signal_id: str, description: str) -> Signal:
    return Signal(signal_id, ValueType.INT64, description=description, default=False, poll_hint_hz=0.0)


TYPE = DeviceType(
    id="bmp3xx",
    signals=(
        Signal("pressure", ValueType.DOUBLE, unit="hPa", description="Barometric pressure"),
        Signal("temperature", ValueType.DOUBLE, unit="C", description="Sensor temperature"),
        Signal(
            "altitude",
            ValueType.DOUBLE,
            unit="m",
            description="Altitude estimated from pressure and the sea-level pressure",
            default=False,
            poll_hint_hz=0.0,
        ),
        Signal(
            "sea_level_pressure",
            ValueType.DOUBLE,
            unit="hPa",
            description="Sea-level pressure the altitude estimate uses",
            default=False,
            poll_hint_hz=0.0,
        ),
        _read_back("pressure_oversampling", "Pressure oversampling, read back from the chip"),
        _read_back("temperature_oversampling", "Temperature oversampling, read back from the chip"),
        _read_back("filter_coefficient", "IIR filter coefficient, read back from the chip"),
    ),
    functions=(
        Function(
            "set_sea_level_pressure",
            description="Set the local sea-level pressure, which calibrates the altitude estimate",
            args=(
                Arg(
                    "sea_level_pressure",
                    ValueType.DOUBLE,
                    min=870.0,
                    max=1085.0,
                    unit="hPa",
                    description="Local sea-level pressure; the recorded extremes bound it",
                ),
            ),
            category=Category.CONFIG,
            is_idempotent=True,
        ),
        _setting("set_pressure_oversampling", "oversampling", _OVERSAMPLING, "Set the pressure oversampling"),
        _setting("set_temperature_oversampling", "oversampling", _OVERSAMPLING, "Set the temperature oversampling"),
        _setting("set_filter_coefficient", "coefficient", _FILTER, "Set the IIR filter coefficient"),
    ),
)


class BMP3xx(DeviceDriver):
    type = TYPE
    addresses = (0x77, 0x76)

    def __init__(self, i2c: Any, address: int) -> None:
        import adafruit_bmp3xx

        try:
            self._sensor = adafruit_bmp3xx.BMP3XX_I2C(i2c, address)
        except ValueError as err:
            if not nothing_at(err):
                raise
            raise NotAnswering(f"nothing answers at {address:#04x}") from err
        except RuntimeError as err:
            if not str(err).startswith("Failed to find BMP3XX"):
                raise
            raise NotAnswering(f"no BMP3xx at {address:#04x}: {err}") from err

    @staticmethod
    def fake() -> FakeBMP3xx:
        return FakeBMP3xx()

    def read(self, signal_ids: list[str]) -> Mapping[str, Any]:
        s = self._sensor
        getters: dict[str, Callable[[], float | int]] = {
            "pressure": lambda: float(s.pressure),
            "temperature": lambda: float(s.temperature),
            "altitude": lambda: float(s.altitude),
            "sea_level_pressure": lambda: float(s.sea_level_pressure),
            "pressure_oversampling": lambda: int(s.pressure_oversampling),
            "temperature_oversampling": lambda: int(s.temperature_oversampling),
            "filter_coefficient": lambda: int(s.filter_coefficient),
        }
        return {sid: getters[sid]() for sid in signal_ids}

    def call(self, function: Function, args: Mapping[str, Any]) -> Mapping[str, Any] | None:
        s = self._sensor
        if function.name == "set_sea_level_pressure":
            s.sea_level_pressure = args["sea_level_pressure"]
        elif function.name == "set_pressure_oversampling":
            s.pressure_oversampling = int(args["oversampling"])
        elif function.name == "set_temperature_oversampling":
            s.temperature_oversampling = int(args["oversampling"])
        elif function.name == "set_filter_coefficient":
            s.filter_coefficient = int(args["coefficient"])
        return None
