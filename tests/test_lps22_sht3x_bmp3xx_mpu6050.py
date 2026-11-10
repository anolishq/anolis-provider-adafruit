"""The SHT3x, LPS22, BMP3xx and MPU-6050 boards against their chip models:
readings, the chips they refuse, settings, and chips that restart."""

from __future__ import annotations

import math
from typing import Any

import adafruit_lps2x
import adafruit_mpu6050
import pytest
from anolis_provider_sdk import DeviceState, ProviderError

from anolis_provider_adafruit.devices import KINDS, NotAnswering
from anolis_provider_adafruit.devices.bmp3xx import BMP3xx
from anolis_provider_adafruit.devices.lps22 import LPS22
from anolis_provider_adafruit.devices.mpu6050 import MPU6050
from anolis_provider_adafruit.devices.sht3x import SHT3x
from anolis_provider_adafruit.mock import FakeBMP3xx, FakeI2C, FakeLPS22, FakeMPU6050, FakeSHT3x
from anolis_provider_adafruit.provider import AdafruitProvider


def provider(kind: str, *, address: int | None = None, chip: Any = None) -> AdafruitProvider:
    entry: dict[str, Any] = {"id": "dut", "type": kind}
    if address is not None:
        entry["address"] = address
    p = AdafruitProvider({"mock": True, "devices": [entry]})
    p.start(0.0)
    if chip is not None:
        bus = p._bus
        assert isinstance(bus, FakeI2C)
        bus.chips[address if address is not None else KINDS[kind].addresses[0]] = chip
        p._slots["dut"].driver = None  # connect again, to the chip given
    return p


def function(kind: str, name: str) -> Any:
    return next(f for f in KINDS[kind].type.functions if f.name == name)


def call(kind: str, driver: Any, name: str, **args: Any) -> None:
    driver.call(function(kind, name), args)


@pytest.mark.parametrize("kind", ["sht3x", "lps22", "bmp3xx", "mpu6050"])
def test_each_board_reads_at_every_address_it_can_be_strapped_to(kind: str) -> None:
    for address in KINDS[kind].addresses:
        p = provider(kind, address=address)
        d = p.devices()[0]
        signals = [s.id for s in KINDS[kind].type.signals]
        assert set(p.read(d, signals)) == set(signals)


@pytest.mark.parametrize("kind", ["sht3x", "lps22", "bmp3xx", "mpu6050"])
def test_nothing_at_the_address_is_not_answering(kind: str) -> None:
    with pytest.raises(NotAnswering, match="nothing answers"):
        KINDS[kind](FakeI2C({}), KINDS[kind].addresses[0])


# SHT3x


class GarbledSHT3x(FakeSHT3x):
    on = False

    def read(self, length: int) -> bytes:
        out = super().read(length)
        return bytes(b ^ 0x01 for b in out) if self.on else out


def test_sht3x_readings_follow_the_datasheet_conversion() -> None:
    chip = FakeSHT3x()
    chip.set_measurement(temperature=31.25, humidity=62.5)
    s = SHT3x(FakeI2C({0x44: chip}), 0x44)
    assert s.read(["temperature", "relative_humidity"]) == {
        "temperature": pytest.approx(31.25, abs=0.01),
        "relative_humidity": pytest.approx(62.5, abs=0.01),
    }


def test_an_sht3x_whose_serial_fails_its_crc_is_refused() -> None:
    chip = GarbledSHT3x()
    chip.on = True
    with pytest.raises(NotAnswering, match="serial number"):
        SHT3x(FakeI2C({0x44: chip}), 0x44)


def test_an_sht3x_reading_that_fails_its_crc_is_unavailable_and_the_board_recovers() -> None:
    chip = GarbledSHT3x()
    p = provider("sht3x", chip=chip)
    d = p.devices()[0]
    assert set(p.read(d, ["temperature"])) == {"temperature"}
    chip.on = True
    with pytest.raises(ProviderError, match="CRC"):
        p.read(d, ["temperature"])
    health = p.device_health(d)
    assert health and health.state is DeviceState.UNREACHABLE
    chip.on = False
    assert set(p.read(d, ["temperature"])) == {"temperature"}


# LPS22


def test_lps22_readings_follow_the_datasheet_scaling() -> None:
    chip = FakeLPS22()
    chip.set_measurement(pressure=987.5, temperature=21.75)
    s = LPS22(FakeI2C({0x5D: chip}), 0x5D)
    assert s.read(["pressure", "temperature"]) == {
        "pressure": pytest.approx(987.5, abs=0.001),
        "temperature": pytest.approx(21.75, abs=0.001),
    }


def test_a_chip_with_another_id_is_not_taken_for_an_lps22() -> None:
    class Lps25(FakeLPS22):
        CHIP_ID = 0xBD

    with pytest.raises(NotAnswering, match="no LPS22"):
        LPS22(FakeI2C({0x5D: Lps25()}), 0x5D)


def test_the_lps22_rate_reads_back_from_the_chip() -> None:
    s = LPS22(FakeI2C({0x5D: FakeLPS22()}), 0x5D)
    assert s.read(["data_rate"]) == {"data_rate": 75.0}
    call("lps22", s, "set_data_rate", rate_hz="10")
    assert s.read(["data_rate"]) == {"data_rate": 10.0}


def test_an_lps22_that_restarted_measures_again_at_the_rate_it_was_set_to() -> None:
    chip = FakeLPS22()
    s = LPS22(FakeI2C({0x5D: chip}), 0x5D)
    call("lps22", s, "set_data_rate", rate_hz="25")
    chip.power_cycle()
    chip.set_measurement(pressure=1001.0, temperature=20.0)
    assert s.read(["pressure", "data_rate"]) == {"pressure": pytest.approx(1001.0, abs=0.001), "data_rate": 25.0}


# BMP3xx


def test_bmp3xx_readings_are_plausible_and_pinned() -> None:
    # A made-up calibration, so this pins the driver's conversion: the
    # datasheet has no worked example to check it against.
    s = BMP3xx(FakeI2C({0x77: FakeBMP3xx()}), 0x77)
    values = s.read(["pressure", "temperature"])
    assert values["temperature"] == pytest.approx(24.00, abs=0.01)
    assert values["pressure"] == pytest.approx(1000.00, abs=0.01)


def test_a_chip_with_another_id_is_not_taken_for_a_bmp3xx() -> None:
    class Other(FakeBMP3xx):
        CHIP_ID = 0x58  # a BMP280

    with pytest.raises(NotAnswering, match="no BMP3xx"):
        BMP3xx(FakeI2C({0x77: Other()}), 0x77)


def test_bmp3xx_settings_read_back_from_the_chip() -> None:
    s = BMP3xx(FakeI2C({0x77: FakeBMP3xx()}), 0x77)
    assert s.read(["pressure_oversampling", "temperature_oversampling", "filter_coefficient"]) == {
        "pressure_oversampling": 4,  # the chip's reset value, OSR 0x02
        "temperature_oversampling": 1,
        "filter_coefficient": 0,
    }
    call("bmp3xx", s, "set_pressure_oversampling", oversampling="32")
    call("bmp3xx", s, "set_temperature_oversampling", oversampling="2")
    call("bmp3xx", s, "set_filter_coefficient", coefficient="16")
    assert s.read(["pressure_oversampling", "temperature_oversampling", "filter_coefficient"]) == {
        "pressure_oversampling": 32,
        "temperature_oversampling": 2,
        "filter_coefficient": 16,
    }


def test_the_bmp3xx_altitude_uses_the_sea_level_pressure_set() -> None:
    s = BMP3xx(FakeI2C({0x77: FakeBMP3xx()}), 0x77)
    pressure = s.read(["pressure"])["pressure"]
    call("bmp3xx", s, "set_sea_level_pressure", sea_level_pressure=pressure)
    assert s.read(["altitude", "sea_level_pressure"]) == {
        "altitude": pytest.approx(0.0, abs=0.01),
        "sea_level_pressure": pressure,
    }


def test_a_bmp3xx_that_restarted_still_reads_correctly() -> None:
    chip = FakeBMP3xx()
    s = BMP3xx(FakeI2C({0x77: chip}), 0x77)
    before = s.read(["pressure", "temperature"])
    chip.power_cycle()
    assert s.read(["pressure", "temperature"]) == before


# MPU-6050


def test_mpu6050_readings_scale_with_the_ranges_set() -> None:
    chip = FakeMPU6050()
    s = MPU6050(FakeI2C({0x68: chip}), 0x68)
    chip.set_raw(acceleration=(8192, -8192, 16384), temperature=-3920, gyro=(655, -655, 0))
    values = s.read(["acceleration_x", "acceleration_y", "acceleration_z", "gyro_x", "gyro_y", "temperature"])
    assert values == {
        "acceleration_x": pytest.approx(9.80665 / 2),
        "acceleration_y": pytest.approx(-9.80665 / 2),
        "acceleration_z": pytest.approx(9.80665),
        "gyro_x": pytest.approx(math.radians(10.0)),  # 65.5 LSB per degree/s at the driver's 500 degrees/s
        "gyro_y": pytest.approx(math.radians(-10.0)),
        "temperature": pytest.approx(-3920 / 340 + 36.53),
    }
    call("mpu6050", s, "set_accelerometer_range", range_g="4")
    call("mpu6050", s, "set_gyro_range", range_dps="250")
    assert s.read(["acceleration_x", "gyro_x"]) == {
        "acceleration_x": pytest.approx(9.80665),
        "gyro_x": pytest.approx(math.radians(655 / 131)),
    }


def test_mpu6050_settings_read_back_from_the_chip() -> None:
    s = MPU6050(FakeI2C({0x68: FakeMPU6050()}), 0x68)
    settings = ["accelerometer_range", "gyro_range", "filter_bandwidth"]
    assert s.read(settings) == {"accelerometer_range": "2", "gyro_range": "500", "filter_bandwidth": "260"}
    call("mpu6050", s, "set_accelerometer_range", range_g="16")
    call("mpu6050", s, "set_gyro_range", range_dps="2000")
    call("mpu6050", s, "set_filter_bandwidth", bandwidth_hz="21")
    assert s.read(settings) == {"accelerometer_range": "16", "gyro_range": "2000", "filter_bandwidth": "21"}


def test_a_chip_with_another_id_is_not_taken_for_an_mpu6050() -> None:
    class Other(FakeMPU6050):
        DEVICE_ID = 0x70  # an MPU-6500

    with pytest.raises(NotAnswering, match="no MPU-6050"):
        MPU6050(FakeI2C({0x68: Other()}), 0x68)


def test_an_mpu6050_that_restarted_wakes_and_keeps_its_settings() -> None:
    chip = FakeMPU6050()
    s = MPU6050(FakeI2C({0x68: chip}), 0x68)
    call("mpu6050", s, "set_accelerometer_range", range_g="8")
    chip.power_cycle()
    chip.set_raw(acceleration=(0, 0, 4096), temperature=0, gyro=(0, 0, 0))
    assert s.read(["acceleration_z", "accelerometer_range"]) == {
        "acceleration_z": pytest.approx(9.80665),
        "accelerometer_range": "8",
    }


# Why the LPS22 and MPU-6050 boards check for a restart on every read.


def test_a_restarted_lps22_reads_frozen_through_the_driver_alone() -> None:
    chip = FakeLPS22()
    sensor = adafruit_lps2x.LPS22(FakeI2C({0x5D: chip}), 0x5D)
    chip.power_cycle()
    chip.set_measurement(pressure=1001.0, temperature=20.0)
    assert sensor.pressure == 0.0


def test_a_restarted_mpu6050_reads_frozen_through_the_driver_alone() -> None:
    chip = FakeMPU6050()
    sensor = adafruit_mpu6050.MPU6050(FakeI2C({0x68: chip}), 0x68)
    chip.power_cycle()
    assert sensor.acceleration == (0.0, 0.0, 0.0)
