import fcntl
import json
import logging
import os
import time


def _flock(file, op, timeout, interval=0.02):
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(file, op | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"flock timeout after {timeout}s")
            time.sleep(interval)


class KeyValueCache:
    def __init__(self, cache_path):
        self.path = cache_path

    def _read(self, file):
        try:
            data = json.loads(file.read() or "{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a cache dictionary")
            return data
        except ValueError as e:
            logging.warning("Ignoring invalid cache %s: %s", self.path, e)
            return {}

    def get(self, key):
        try:
            with open(self.path) as file:
                _flock(file, fcntl.LOCK_SH, timeout=1)
                return self._read(file).get(key)
        except FileNotFoundError:
            return None
        except (TimeoutError, OSError) as e:
            logging.warning("Cannot read cache %s: %s", self.path, e)
            return None

    def set(self, key, value):
        try:
            with os.fdopen(
                os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+"
            ) as file:
                _flock(file, fcntl.LOCK_SH, timeout=1)
                data = self._read(file)
                data[key] = value
                file.seek(0)
                json.dump(data, file)
                file.truncate()
        except OSError as e:
            logging.warning("Cannot write cache %s: %s", self.path, e)
