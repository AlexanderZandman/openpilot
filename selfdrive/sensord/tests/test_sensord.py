#!/usr/bin/env python3
import os
import time
import unittest
import numpy as np
from collections import namedtuple

import cereal.messaging as messaging
from cereal import log
from system.hardware import TICI, HARDWARE
from selfdrive.manager.process_config import managed_processes

SENSOR_CONFIGURATIONS = (
  {
    ('bmx055', 'acceleration'),
    ('bmx055', 'gyroUncalibrated'),
    ('bmx055', 'magneticUncalibrated'),
    ('bmx055', 'temperature'),
    ('lsm6ds3', 'acceleration'),
    ('lsm6ds3', 'gyroUncalibrated'),
    ('lsm6ds3', 'temperature'),
    ('rpr0521', 'light'),
  },
  {
    ('lsm6ds3', 'acceleration'),
    ('lsm6ds3', 'gyroUncalibrated'),
    ('lsm6ds3', 'temperature'),
    ('mmc5603nj', 'magneticUncalibrated'),
    ('rpr0521', 'light'),
  },
  {
    ('bmx055', 'acceleration'),
    ('bmx055', 'gyroUncalibrated'),
    ('bmx055', 'magneticUncalibrated'),
    ('bmx055', 'temperature'),
    ('lsm6ds3trc', 'acceleration'),
    ('lsm6ds3trc', 'gyroUncalibrated'),
    ('lsm6ds3trc', 'temperature'),
    ('rpr0521', 'light'),
  },
  {
    ('lsm6ds3trc', 'acceleration'),
    ('lsm6ds3trc', 'gyroUncalibrated'),
    ('lsm6ds3trc', 'temperature'),
    ('mmc5603nj', 'magneticUncalibrated'),
    ('rpr0521', 'light'),
  },
)

Sensor = log.SensorEventData.SensorSource
SensorConfig = namedtuple('SensorConfig', ['type', 'sanity_min', 'sanity_max'])
ALL_SENSORS = {
  Sensor.rpr0521: {
    SensorConfig("light", 0, 1023),
  },

  Sensor.lsm6ds3: {
    SensorConfig("acceleration", 5, 15),
    SensorConfig("gyroUncalibrated", 0, .2),
    SensorConfig("temperature", 0, 60),
  },

  Sensor.lsm6ds3trc: {
    SensorConfig("acceleration", 5, 15),
    SensorConfig("gyroUncalibrated", 0, .2),
    SensorConfig("temperature", 0, 60),
  },

  Sensor.bmx055: {
    SensorConfig("acceleration", 5, 15),
    SensorConfig("gyroUncalibrated", 0, .2),
    SensorConfig("magneticUncalibrated", 0, 300),
    SensorConfig("temperature", 0, 60),
  },

  Sensor.mmc5603nj: {
    SensorConfig("magneticUncalibrated", 0, 300),
  }
}

SENSOR_BUS = 1
I2C_ADDR_LSM = 0x6A
LSM_INT_GPIO = 84

def read_sensor_events(duration_sec):
  sensor_events = messaging.sub_sock("sensorEvents", timeout=0.1)
  start_time_sec = time.monotonic()
  events = []
  while time.monotonic() - start_time_sec < duration_sec:
    events += messaging.drain_sock(sensor_events)
    time.sleep(0.01)

  assert len(events) != 0, "No sensor events collected"
  return events

def get_proc_interrupts(int_pin):
  with open("/proc/interrupts") as f:
    lines = f.read().split("\n")

  for line in lines:
    if f" {int_pin} " in line:
      return ''.join(list(filter(lambda e: e != '', line.split(' ')))[1:6])

  return ""


class TestSensord(unittest.TestCase):
  @classmethod
  def setUpClass(cls):
    if not TICI:
      raise unittest.SkipTest

    # make sure gpiochip0 is readable
    HARDWARE.initialize_hardware()

    # read initial sensor values every test case can use
    os.system("pkill -f ./_sensord")
    managed_processes["sensord"].start()
    time.sleep(3)
    cls.sample_secs = 10
    cls.events = read_sensor_events(cls.sample_secs)
    managed_processes["sensord"].stop()

  @classmethod
  def tearDownClass(cls):
    managed_processes["sensord"].stop()

  def tearDown(self):
    managed_processes["sensord"].stop()

  def test_sensors_present(self):
    # verify correct sensors configuration

    seen = set()
    for event in self.events:
      for measurement in event.sensorEvents:
        # filter unset events (bmx magn)
        if measurement.version == 0:
          continue
        seen.add((str(measurement.source), measurement.which()))

    self.assertIn(seen, SENSOR_CONFIGURATIONS)

  def test_lsm6ds3_timing(self):
    # verify measurements are sampled and published at 104Hz

    sensor_t = {
      1: [], # accel
      5: [], # gyro
    }
    for event in self.events:
      for measurement in event.sensorEvents:
        if str(measurement.source).startswith("lsm6ds3") and measurement.sensor in sensor_t:
          sensor_t[measurement.sensor].append(measurement.timestamp)

    for s, vals in sensor_t.items():
      with self.subTest(sensor=s):
        assert len(vals) > 0
        tdiffs = np.diff(vals) / 1e6 # millis

        high_delay_diffs = list(filter(lambda d: d >= 20., tdiffs))
        assert len(high_delay_diffs) < 15, f"Too many large diffs: {high_delay_diffs}"

        # 100-108Hz, expected 104Hz
        avg_diff = sum(tdiffs)/len(tdiffs)
        assert 9.3 < avg_diff < 10., f"avg difference {avg_diff}, below threshold"

        stddev = np.std(tdiffs)
        assert stddev < 2.0, f"Standard-dev to big {stddev}"

  def test_events_check(self):
    # verify if all sensors produce events

    sensor_events = dict()
    for event in  self.events:
      for measurement in event.sensorEvents:

        # filter unset events (bmx magn)
        if measurement.version == 0:
          continue

        if measurement.type in sensor_events:
          sensor_events[measurement.type] += 1
        else:
          sensor_events[measurement.type] = 1

    for s in sensor_events:
      err_msg = f"Sensor {s}: 200 < {sensor_events[s]}"
      assert sensor_events[s] > 200, err_msg

  def test_logmonottime_timestamp_diff(self):
    # ensure diff between the message logMonotime and sample timestamp is small

    tdiffs = list()
    for event in self.events:
      for measurement in event.sensorEvents:

        # filter unset events (bmx magn)
        if measurement.version == 0:
          continue

        # check if gyro and accel timestamps are before logMonoTime
        if str(measurement.source).startswith("lsm6ds3"):
          if measurement.which() != 'temperature':
            err_msg = f"Timestamp after logMonoTime: {measurement.timestamp} > {event.logMonoTime}"
            assert measurement.timestamp < event.logMonoTime, err_msg

        # negative values might occur, as non interrupt packages created
        # before the sensor is read
        tdiffs.append(abs(event.logMonoTime - measurement.timestamp))

    high_delay_diffs = set(filter(lambda d: d >= 10*10**6, tdiffs))
    assert len(high_delay_diffs) < 15, f"Too many high delay packages: {high_delay_diffs}"

    avg_diff = round(sum(tdiffs)/len(tdiffs), 4)
    assert avg_diff < 4*10**6, f"Avg packet diff: {avg_diff:.1f}ns"

    stddev = np.std(tdiffs)
    assert stddev < 2*10**6, f"Timing diffs have to high stddev: {stddev}"

  def test_sensor_values_sanity_check(self):
    sensor_values = dict()
    for event in self.events:
      for m in event.sensorEvents:

        # filter unset events (bmx magn)
        if m.version == 0:
          continue

        key = (m.source.raw, m.which())
        values = getattr(m, m.which())
        if hasattr(values, 'v'):
          values = values.v
        values = np.atleast_1d(values)

        if key in sensor_values:
          sensor_values[key].append(values)
        else:
          sensor_values[key] = [values]

    # Sanity check sensor values and counts
    for sensor, stype in sensor_values:

      for s in ALL_SENSORS[sensor]:
        if s.type != stype:
          continue

        key = (sensor, s.type)
        val_cnt = len(sensor_values[key])
        min_samples = self.sample_secs * 100  # Hz
        err_msg = f"Sensor {sensor} {s.type} got {val_cnt} measurements, expected {min_samples}"
        assert min_samples*0.9 < val_cnt < min_samples*1.1, err_msg

        mean_norm = np.mean(np.linalg.norm(sensor_values[key], axis=1))
        err_msg = f"Sensor '{sensor} {s.type}' failed sanity checks {mean_norm} is not between {s.sanity_min} and {s.sanity_max}"
        assert s.sanity_min <= mean_norm <= s.sanity_max, err_msg

  def test_sensor_verify_no_interrupts_after_stop(self):

    managed_processes["sensord"].start()
    time.sleep(3)

    # read /proc/interrupts to verify interrupts are received
    state_one = get_proc_interrupts(LSM_INT_GPIO)
    time.sleep(1)
    state_two = get_proc_interrupts(LSM_INT_GPIO)

    assert state_one != state_two, f"no interrupts received after sensord start!\n{state_one} {state_two}"

    managed_processes["sensord"].stop()
    time.sleep(1)

    # read /proc/interrupts to verify no more interrupts are received
    state_one = get_proc_interrupts(LSM_INT_GPIO)
    time.sleep(1)
    state_two = get_proc_interrupts(LSM_INT_GPIO)
    assert state_one == state_two, "Interrupts received after sensord stop!"


if __name__ == "__main__":
  unittest.main()
