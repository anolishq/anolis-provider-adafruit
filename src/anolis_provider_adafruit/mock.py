"""A fake I2C bus for mock mode, CI and tests.

It implements the bus interface the Adafruit drivers call (busio.I2C: try_lock,
unlock, writeto, readfrom_into, writeto_then_readfrom, scan) and puts a register
model of each chip behind it. The real drivers run unchanged against it, so mock
mode exercises the same driver code as the hardware does, from the first
register read on.
"""

from __future__ import annotations

import struct
import time
from collections.abc import Mapping
from typing import Protocol


class FakeChip(Protocol):
    def write(self, data: bytes) -> None: ...
    def read(self, length: int) -> bytes: ...


class RegisterChip:
    """A chip with an 8-bit register pointer: a write sets the pointer and
    writes any following bytes from it; a read returns bytes from the pointer.
    The pointer advances with each byte, as on most I2C register chips."""

    def __init__(self) -> None:
        self.registers = bytearray(256)
        self._pointer = 0

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._pointer = data[0]
        for byte in data[1:]:
            self.on_write(self._pointer, byte)
            self._pointer = self.next_register(self._pointer)

    def read(self, length: int) -> bytes:
        out = bytearray()
        for _ in range(length):
            out.append(self.registers[self._pointer])
            self._pointer = self.next_register(self._pointer)
        return bytes(out)

    def next_register(self, register: int) -> int:
        """Where the pointer goes after a byte is transferred."""
        return (register + 1) & 0xFF

    def on_write(self, register: int, value: int) -> None:
        self.registers[register] = value


class FakeBME280(RegisterChip):
    """A BME280 holding the calibration and raw readings of the Bosch datasheet's
    worked example (section 8.1): 25.08 C and 1006.53 hPa through the driver's
    compensation. The humidity calibration is a typical part's, giving about 45 %.

    Modelled because a driver or the provider can get each of them wrong:

    - Power-on (and a soft reset, 0xB6 to 0xE0) leaves the chip in sleep mode
      with every setting 0, and the data registers at their reset values,
      0x80000 for temperature and pressure and 0x8000 for humidity. Those
      convert to plausible-looking numbers, so a restarted chip that nobody
      sets up again reads wrong rather than failing.
    - A measurement runs only outside sleep mode, and only for a quantity whose
      oversampling is not 0 ("skipped"); a skipped one keeps its reset value.
      The humidity setting in ctrl_hum takes effect at the next write to
      ctrl_meas (datasheet 5.4.3). Forced mode measures once and returns to
      sleep.

    - Entering normal mode starts the first measurement, which takes up to
      t_measure (datasheet 9.1, the maximum for the oversampling set). Until
      it completes, the data registers keep the values they had.

    Otherwise the model measures instantly, and the status register stays 0: a
    read in normal mode returns the latest raw values set with ``set_raw``.
    """

    CHIP_ID_REG, RESET_REG = 0xD0, 0xE0
    CTRL_HUM, CTRL_MEAS, CONFIG = 0xF2, 0xF4, 0xF5
    DATA = 0xF7  # press_msb .. hum_lsb, 0xF7 to 0xFE
    CHIP_ID = 0x60
    SKIPPED_TP, SKIPPED_H = 0x80000, 0x8000

    def __init__(self) -> None:
        super().__init__()
        self.raw = {"temperature": 519888, "pressure": 415148, "humidity": 28000}
        self.power_cycle()

    def power_cycle(self) -> None:
        """Every register at its power-on value, as at start-up, after a soft
        reset, or after a brown-out."""
        r = self.registers
        r[:] = bytes(256)
        r[self.CHIP_ID_REG] = self.CHIP_ID
        # dig_T1..T3, dig_P1..P9 from 0x88, little-endian.
        r[0x88:0xA0] = struct.pack(
            "<HhhHhhhhhhhh", 27504, 26435, -1000, 36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000
        )
        r[0xA1] = 75  # dig_H1
        # dig_H2 = 362, dig_H3 = 0, dig_H4 = 313, dig_H5 = 50, dig_H6 = 30, packed as the chip packs them.
        r[0xE1:0xE8] = struct.pack("<hBbBbb", 362, 0, 313 >> 4, ((50 & 0xF) << 4) | (313 & 0xF), 50 >> 4, 30)
        self._humidity_on = False
        self._first_due = 0.0  # when the first measurement in normal mode completes
        self._store(self.SKIPPED_TP, self.SKIPPED_TP, self.SKIPPED_H)

    def set_raw(self, *, temperature: int, pressure: int, humidity: int) -> None:
        """Raw ADC values the next measurement returns: 20-bit temperature and
        pressure, 16-bit humidity."""
        self.raw = {"temperature": temperature, "pressure": pressure, "humidity": humidity}

    def _store(self, temperature: int, pressure: int, humidity: int) -> None:
        self.registers[0xF7:0xFA] = (pressure << 4).to_bytes(3, "big")
        self.registers[0xFA:0xFD] = (temperature << 4).to_bytes(3, "big")
        self.registers[0xFD:0xFF] = humidity.to_bytes(2, "big")

    def t_measure_s(self) -> float:
        """The longest a measurement takes at the oversampling set (datasheet 9.1)."""
        oversampling = [0, 1, 2, 4, 8, 16, 16, 16]
        t = oversampling[self.registers[self.CTRL_MEAS] >> 5]
        p = oversampling[(self.registers[self.CTRL_MEAS] >> 2) & 0b111]
        h = oversampling[self.registers[self.CTRL_HUM] & 0b111]
        ms = 1.25 + 2.3 * t + (2.3 * p + 0.575 if p else 0) + (2.3 * h + 0.575 if h else 0)
        return ms / 1000

    def _measure(self) -> None:
        meas = self.registers[self.CTRL_MEAS]
        if meas & 0b11 == 0:  # sleep: the data registers hold their last values
            return
        if time.monotonic() < self._first_due:
            return
        r = self.raw
        self._store(
            r["temperature"] if meas >> 5 else self.SKIPPED_TP,
            r["pressure"] if (meas >> 2) & 0b111 else self.SKIPPED_TP,
            r["humidity"] if self._humidity_on else self.SKIPPED_H,
        )
        if meas & 0b11 != 0b11:  # forced: one measurement, then back to sleep
            self.registers[self.CTRL_MEAS] = meas & ~0b11

    def on_write(self, register: int, value: int) -> None:
        if register == self.RESET_REG:
            if value == 0xB6:
                self.power_cycle()
            return
        if register not in (self.CTRL_HUM, self.CTRL_MEAS, self.CONFIG):
            return  # the id, the status, the data and the calibration are read-only
        was_normal = self.registers[self.CTRL_MEAS] & 0b11 == 0b11
        super().on_write(register, value)
        if register == self.CTRL_MEAS:
            self._humidity_on = bool(self.registers[self.CTRL_HUM] & 0b111)
            if value & 0b11 == 0b11 and not was_normal:
                self._first_due = time.monotonic() + self.t_measure_s()
            if value & 0b11 not in (0b00, 0b11):
                self._measure()

    def read(self, length: int) -> bytes:
        if self._pointer in (self.DATA, 0xFA, 0xFD) and self.registers[self.CTRL_MEAS] & 0b11 == 0b11:
            self._measure()
        return super().read(length)


class FakePCA9685(RegisterChip):
    """A PCA9685 16-channel PWM controller, at its power-on register values
    (NXP datasheet rev. 4, section 7.3): asleep, every output fully off.

    Modelled because a driver can get each of them wrong: the pointer advances
    only with MODE1.AI set, PRE_SCALE takes a write only while MODE1.SLEEP is
    set, and RESTART clears itself."""

    MODE1, MODE2, ALLCALLADR, LED0, PRE_SCALE = 0x00, 0x01, 0x05, 0x06, 0xFE
    AI, SLEEP, RESTART, ALLCALL = 0x20, 0x10, 0x80, 0x01

    def __init__(self) -> None:
        super().__init__()
        self.power_cycle()

    def power_cycle(self) -> None:
        """Every register at its power-on value, as at start-up or after the
        board restarts on a glitch in its logic supply."""
        r = self.registers
        r[:] = bytes(256)
        self._pointer = 0
        r[self.MODE1] = self.SLEEP | self.ALLCALL
        r[self.MODE2] = 0x04  # OUTDRV
        r[self.ALLCALLADR] = 0xE0  # 0x70, as an 8-bit write address
        for channel in range(16):
            r[self.LED0 + 4 * channel + 3] = 0x10  # LEDn_OFF_H: full off
        r[self.PRE_SCALE] = 0x1E  # 200 Hz

    def all_call_address(self) -> int | None:
        """The 7-bit All Call address the chip also answers, while MODE1.ALLCALL is set."""
        if self.registers[self.MODE1] & self.ALLCALL:
            return self.registers[self.ALLCALLADR] >> 1
        return None

    def next_register(self, register: int) -> int:
        if self.registers[self.MODE1] & self.AI:
            return super().next_register(register)
        return register

    def on_write(self, register: int, value: int) -> None:
        if register == self.PRE_SCALE and not self.registers[self.MODE1] & self.SLEEP:
            return
        if register == self.MODE1:
            value &= ~self.RESTART
        super().on_write(register, value)

    def duty(self, channel: int) -> tuple[int, int]:
        """A channel's (LEDn_ON, LEDn_OFF) words, as the driver writes them."""
        base = self.LED0 + 4 * channel
        on, off = struct.unpack("<HH", self.registers[base : base + 4])
        return on, off


def sensirion_crc(data: bytes) -> int:
    """CRC-8 over a 16-bit word, as Sensirion sensors send it: polynomial 0x31,
    initial value 0xFF (0xBEEF gives 0x92)."""
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) if crc & 0x80 else crc << 1
    return crc & 0xFF


def sensirion_words(*words: int) -> bytes:
    """16-bit words, each followed by its CRC, as a Sensirion sensor replies."""
    out = bytearray()
    for w in words:
        pair = w.to_bytes(2, "big")
        out += pair + bytes([sensirion_crc(pair)])
    return bytes(out)


class _SensirionHumidity:
    """What the SHT4x and SHTC3 have in common: a command, then a read of the
    reply; a read with no reply pending is not acknowledged."""

    def __init__(self) -> None:
        self.temperature, self.humidity = 22.5, 40.0
        self._reply = b""

    def set_measurement(self, *, temperature: float, humidity: float) -> None:
        self.temperature, self.humidity = temperature, humidity

    def read(self, length: int) -> bytes:
        if not self._reply:
            raise OSError(121, "Remote I/O error")
        # Bytes read past the reply see the idle bus: 0xFF.
        out, self._reply = self._reply[:length].ljust(length, b"\xff"), b""
        return out


class FakeSHT4x(_SensirionHumidity):
    """A Sensirion SHT40/41/45 (datasheet version 6.6): one-byte commands, a
    measurement reply of temperature and humidity words with CRCs, and a
    32-bit serial number. Raw values follow the datasheet's conversion,
    T = -45 + 175 * raw / 65535 and RH = -6 + 125 * raw / 65535.

    The heater commands are not modelled: the provider never sends them.
    """

    MEASURE_HIGH, SERIAL, SOFT_RESET = 0xFD, 0x89, 0x94
    SERIAL_NUMBER = 0x12345678

    def write(self, data: bytes) -> None:
        if not data:  # I2CDevice's probe: an empty write, acknowledged
            return
        command = data[0]
        self._reply = b""
        if command == self.MEASURE_HIGH:
            t = round((self.temperature + 45) * 65535 / 175)
            rh = round((self.humidity + 6) * 65535 / 125)
            self._reply = sensirion_words(max(0, min(t, 0xFFFF)), max(0, min(rh, 0xFFFF)))
        elif command == self.SERIAL:
            self._reply = sensirion_words(self.SERIAL_NUMBER >> 16, self.SERIAL_NUMBER & 0xFFFF)
        elif command != self.SOFT_RESET:
            raise OSError(121, "Remote I/O error")


class FakeSHTC3(_SensirionHumidity):
    """A Sensirion SHTC3 (datasheet version 4): two-byte commands, and a sleep
    mode in which it acknowledges nothing but the wake-up command. Power-on
    leaves it awake. Raw values follow the datasheet's conversion,
    T = -45 + 175 * raw / 65536 and RH = 100 * raw / 65536."""

    WAKEUP, SLEEP, SOFT_RESET, READ_ID = 0x3517, 0xB098, 0x805D, 0xEFC8
    MEASURE_NORMAL_T_FIRST, MEASURE_LOWPOWER_T_FIRST = 0x7866, 0x609C
    ID = 0x0887  # the bits the datasheet defines read 0x0807

    def __init__(self) -> None:
        super().__init__()
        self.asleep = False

    def write(self, data: bytes) -> None:
        if not data:  # I2CDevice's probe: an empty write, acknowledged even asleep
            return
        command = int.from_bytes(data[:2], "big")
        if self.asleep and command != self.WAKEUP:
            raise OSError(121, "Remote I/O error")
        self._reply = b""
        if command == self.WAKEUP:
            self.asleep = False
        elif command == self.SLEEP:
            self.asleep = True
        elif command == self.READ_ID:
            self._reply = sensirion_words(self.ID)
        elif command in (self.MEASURE_NORMAL_T_FIRST, self.MEASURE_LOWPOWER_T_FIRST):
            t = round((self.temperature + 45) * 65536 / 175)
            rh = round(self.humidity * 65536 / 100)
            self._reply = sensirion_words(max(0, min(t, 0xFFFF)), max(0, min(rh, 0xFFFF)))
        elif command != self.SOFT_RESET:
            raise OSError(121, "Remote I/O error")


class FakeSHT3x(_SensirionHumidity):
    """A Sensirion SHT31 (datasheet version 7): two-byte commands, a measurement
    reply of temperature and humidity words with CRCs, and a 32-bit serial
    number. Raw values follow the datasheet's conversion,
    T = -45 + 175 * raw / 65535 and RH = 100 * raw / 65535.

    Only the commands the driver sends for a single-shot, high-repeatability
    measurement with no clock stretching are modelled."""

    BREAK, SOFT_RESET, SERIAL, MEASURE_HIGH = 0x3093, 0x30A2, 0x3780, 0x2400
    SERIAL_NUMBER = 0x12345678

    def write(self, data: bytes) -> None:
        if not data:  # I2CDevice's probe: an empty write, acknowledged
            return
        command = int.from_bytes(data[:2], "big")
        self._reply = b""
        if command == self.MEASURE_HIGH:
            t = round((self.temperature + 45) * 65535 / 175)
            rh = round(self.humidity * 65535 / 100)
            self._reply = sensirion_words(max(0, min(t, 0xFFFF)), max(0, min(rh, 0xFFFF)))
        elif command == self.SERIAL:
            self._reply = sensirion_words(self.SERIAL_NUMBER >> 16, self.SERIAL_NUMBER & 0xFFFF)
        elif command not in (self.BREAK, self.SOFT_RESET):
            raise OSError(121, "Remote I/O error")


class FakeLPS22(RegisterChip):
    """An ST LPS22HB. Pressure is 4096 LSB/hPa in 24 bits and temperature 100
    LSB/C in 16 bits, both little-endian.

    Modelled because a driver or the provider can get each of them wrong:

    - The top bit of the register address asks for auto-increment and is not
      part of the address.
    - Power-on and a software reset (CTRL_REG2 bit 2, which clears itself)
      leave the chip in one-shot mode with its data registers at 0.
    - In one-shot mode nothing is measured, so the data registers hold their
      last values and a restarted chip reads frozen rather than failing.
    """

    WHO_AM_I, CTRL_REG1, CTRL_REG2, DATA = 0x0F, 0x10, 0x11, 0x28
    CHIP_ID = 0xB1
    SWRESET, IF_ADD_INC = 0x04, 0x10

    def __init__(self) -> None:
        super().__init__()
        self.set_measurement(pressure=1013.25, temperature=22.5)
        self.power_cycle()

    def power_cycle(self) -> None:
        """Every register at its power-on value, as at start-up, after a
        software reset, or after a brown-out."""
        r = self.registers
        r[:] = bytes(256)
        r[self.WHO_AM_I] = self.CHIP_ID
        r[self.CTRL_REG2] = self.IF_ADD_INC

    def set_measurement(self, *, pressure: float, temperature: float) -> None:
        self.raw_pressure, self.raw_temperature = round(pressure * 4096), round(temperature * 100)

    def write(self, data: bytes) -> None:
        if data:
            super().write(bytes([data[0] & 0x7F]) + data[1:])

    def on_write(self, register: int, value: int) -> None:
        if register == self.CTRL_REG2 and value & self.SWRESET:
            self.power_cycle()
        elif register in (self.CTRL_REG1, self.CTRL_REG2):
            super().on_write(register, value)

    def read(self, length: int) -> bytes:
        if self.registers[self.CTRL_REG1] >> 4 & 0b111 and self.DATA <= self._pointer < self.DATA + 5:
            self.registers[self.DATA : self.DATA + 3] = (self.raw_pressure & 0xFFFFFF).to_bytes(3, "little")
            self.registers[self.DATA + 3 : self.DATA + 5] = struct.pack("<h", self.raw_temperature)
        return super().read(length)


class FakeBMP3xx(RegisterChip):
    """A BMP390 (datasheet BST-BMP390-DS002). The calibration is made up, chosen
    to give about 24 C and 1000 hPa: the datasheet gives no worked example, so
    the readings pin the driver's conversion rather than check it against
    Bosch's numbers.

    Modelled because a driver can get each of them wrong: power-on and the soft
    reset command leave the chip asleep and OSR at 0x00 (the datasheet's register
    table says 0x02, but a BMP390 read 0x00 after a soft reset); a forced-mode write to
    PWR_CTRL measures once, only the quantities enabled, and returns to sleep;
    the data-ready bits are set by a measurement and cleared by reading the
    data."""

    CHIP_ID_REG, STATUS, DATA, PWR_CTRL, OSR, CONFIG, CALIBRATION, CMD = 0x00, 0x03, 0x04, 0x1B, 0x1C, 0x1F, 0x31, 0x7E
    CHIP_ID = 0x60
    CMD_RDY, DRDY_PRESS, DRDY_TEMP = 0x10, 0x20, 0x40
    FORCED = 0b01

    def __init__(self) -> None:
        super().__init__()
        self.set_raw(pressure=9234259, temperature=8469940)
        self.power_cycle()

    def set_raw(self, *, pressure: int, temperature: int) -> None:
        """24-bit ADC values the next measurement returns."""
        self.raw_pressure, self.raw_temperature = pressure, temperature

    def power_cycle(self) -> None:
        """Every register at its power-on value (datasheet section 4.2), as at
        start-up or after a soft reset."""
        r = self.registers
        r[:] = bytes(256)
        r[self.CHIP_ID_REG] = self.CHIP_ID
        r[self.STATUS] = self.CMD_RDY
        r[self.CALIBRATION : self.CALIBRATION + 21] = struct.pack(
            "<HHbhhbbHHbbhbb", 27772, 18996, -10, 20000, -5352, 6, 0, 9000, 3000, -8, -2, 17000, 20, -60
        )

    def on_write(self, register: int, value: int) -> None:
        if register == self.CMD:
            if value == 0xB6:
                self.power_cycle()
        elif register == self.PWR_CTRL:
            self.registers[register] = value
            if value >> 4 & 0b11 == self.FORCED:
                self._measure(value)
        elif register in (self.OSR, self.CONFIG):
            super().on_write(register, value)

    def _measure(self, control: int) -> None:
        r = self.registers
        if control & 0b01:
            r[self.DATA : self.DATA + 3] = self.raw_pressure.to_bytes(3, "little")
            r[self.STATUS] |= self.DRDY_PRESS
        if control & 0b10:
            r[self.DATA + 3 : self.DATA + 6] = self.raw_temperature.to_bytes(3, "little")
            r[self.STATUS] |= self.DRDY_TEMP
        r[self.PWR_CTRL] = control & 0b11  # back to sleep

    def read(self, length: int) -> bytes:
        data_read = self._pointer == self.DATA
        out = super().read(length)
        if data_read:
            self.registers[self.STATUS] &= ~(self.DRDY_PRESS | self.DRDY_TEMP)
        return out


class FakeMPU6050(RegisterChip):
    """An MPU-6050 (register map revision 4.2). Readings are big-endian signed
    16-bit raw values; temperature is raw / 340 + 36.53.

    Modelled because a driver or the provider can get each of them wrong:

    - Power-on and DEVICE_RESET (PWR_MGMT_1 bit 7, which clears itself) leave
      every register 0 but WHO_AM_I and PWR_MGMT_1, whose sleep bit is set.
    - Asleep, the data registers hold their last values, so a restarted chip
      reads frozen rather than failing.
    - Asleep, only PWR_MGMT_1 takes a write. Measured on GYRO_CONFIG with an
      Adafruit 3886: a write asleep left it 0, the same write awake took.
    - WHO_AM_I reads 0x68 whichever address AD0 straps the chip to.
    """

    ACCEL_OUT, TEMP_OUT, GYRO_OUT, SIGNAL_PATH_RESET, PWR_MGMT_1, WHO_AM_I = 0x3B, 0x41, 0x43, 0x68, 0x6B, 0x75
    DEVICE_ID = 0x68
    DEVICE_RESET, SLEEP = 0x80, 0x40

    def __init__(self) -> None:
        super().__init__()
        self.set_raw(acceleration=(0, 0, 16384), temperature=-3920, gyro=(0, 0, 0))
        self.power_cycle()

    def power_cycle(self) -> None:
        """Every register at its power-on value, as at start-up, after a device
        reset, or after a brown-out."""
        r = self.registers
        r[:] = bytes(256)
        r[self.WHO_AM_I] = self.DEVICE_ID
        r[self.PWR_MGMT_1] = self.SLEEP

    def set_raw(self, *, acceleration: tuple[int, int, int], temperature: int, gyro: tuple[int, int, int]) -> None:
        self.raw = struct.pack(">hhhhhhh", *acceleration, temperature, *gyro)

    def on_write(self, register: int, value: int) -> None:
        if register == self.PWR_MGMT_1 and value & self.DEVICE_RESET:
            self.power_cycle()
        elif register == self.PWR_MGMT_1:
            super().on_write(register, value)
        elif self.registers[self.PWR_MGMT_1] & self.SLEEP or register == self.SIGNAL_PATH_RESET:
            pass  # asleep, or a register that clears itself
        elif register not in range(self.ACCEL_OUT, self.GYRO_OUT + 6) and register != self.WHO_AM_I:
            super().on_write(register, value)

    def read(self, length: int) -> bytes:
        asleep = self.registers[self.PWR_MGMT_1] & self.SLEEP
        if not asleep and self.ACCEL_OUT <= self._pointer < self.GYRO_OUT + 6:
            self.registers[self.ACCEL_OUT : self.GYRO_OUT + 6] = self.raw
        return super().read(length)


class FakeI2C:
    """A bus with fake chips at fixed addresses. An address with no chip raises
    OSError, as Linux reports a NACK, so a driver's probe fails as on hardware.

    A PCA9685 with All Call enabled (its power-on state) also answers its All
    Call address, 0x70, as on hardware: it takes every write sent there, and a
    read there gets the wired AND of every chip that answers it. Only a
    transfer that no chip acknowledges fails."""

    def __init__(self, chips: Mapping[int, FakeChip]) -> None:
        self.chips = dict(chips)
        self._locked = False
        self.fail = False  # set to make every transfer fail, as a disconnected bus does
        self.writes = 0  # writes so far, register-pointer writes before a read included
        # Set to make the write with this index fail, and only that one: a
        # transfer that breaks partway through a sequence of writes.
        self.fail_write: int | None = None

    def _chips(self, address: int) -> list[FakeChip]:
        if self.fail:
            raise OSError(5, "Input/output error")
        own = self.chips.get(address)
        chips = [] if own is None else [own]
        chips += [c for c in self.chips.values() if isinstance(c, FakePCA9685) and c.all_call_address() == address]
        if not chips:
            raise OSError(121, "Remote I/O error")
        return chips

    def try_lock(self) -> bool:
        if self._locked:
            return False
        self._locked = True
        return True

    def unlock(self) -> None:
        self._locked = False

    def scan(self) -> list[int]:
        return sorted(self.chips)

    def writeto(self, address: int, buffer: bytes, *, start: int = 0, end: int | None = None) -> None:
        index, self.writes = self.writes, self.writes + 1
        if index == self.fail_write:
            raise OSError(5, "Input/output error")
        data = bytes(buffer[start:end])
        chips = self._chips(address)
        refused: list[OSError] = []
        for chip in chips:
            try:
                chip.write(data)
            except OSError as err:
                refused.append(err)
        if len(refused) == len(chips):
            raise refused[0]

    def readfrom_into(self, address: int, buffer: bytearray, *, start: int = 0, end: int | None = None) -> None:
        end = len(buffer) if end is None else end
        replies: list[bytes] = []
        refused: list[OSError] = []
        for chip in self._chips(address):
            try:
                replies.append(chip.read(end - start))
            except OSError as err:
                refused.append(err)
        if not replies:
            raise refused[0]
        wired_and = replies[0]
        for reply in replies[1:]:
            wired_and = bytes(a & b for a, b in zip(wired_and, reply, strict=True))
        buffer[start:end] = wired_and

    def writeto_then_readfrom(
        self,
        address: int,
        buffer_out: bytes,
        buffer_in: bytearray,
        *,
        out_start: int = 0,
        out_end: int | None = None,
        in_start: int = 0,
        in_end: int | None = None,
    ) -> None:
        self.writeto(address, buffer_out, start=out_start, end=out_end)
        self.readfrom_into(address, buffer_in, start=in_start, end=in_end)

    def deinit(self) -> None:
        pass
