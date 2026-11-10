"""The boards this provider supports, by the ``type`` a config names them with."""

from __future__ import annotations

from .base import DeviceDriver, NotAnswering
from .bme280 import BME280
from .bmp3xx import BMP3xx
from .lps22 import LPS22
from .motor_hat import MotorHat
from .mpu6050 import MPU6050
from .sht3x import SHT3x
from .sht4x import SHT4x
from .shtc3 import SHTC3

KINDS: dict[str, type[DeviceDriver]] = {
    "bme280": BME280,
    "bmp3xx": BMP3xx,
    "lps22": LPS22,
    "motor_hat": MotorHat,
    "mpu6050": MPU6050,
    "sht3x": SHT3x,
    "sht4x": SHT4x,
    "shtc3": SHTC3,
}

__all__ = ["KINDS", "DeviceDriver", "NotAnswering"]
