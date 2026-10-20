import fcntl
import json
import logging
import os


class KeyValueCache:
    def __init__(self, cache_path):
        self.path = cache_path

    @staticmethod
    def _read(file):
        try:
            data = json.loads(file.read() or "{}")
            if not isinstance(data, dict):
                raise ValueError("Expected a cache dictionary")
            return data
        except ValueError as e:
            logging.warning("Ignoring invalid cache %s: %s", file.name, e)
            return {}

    def get(self, key):
        try:
            with open(self.path) as file:
                fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
                return self._read(file).get(key)
        except FileNotFoundError:
            return None
        except OSError as e:
            logging.warning("Cannot read cache %s: %s", self.path, e)
            return None

    def set(self, key, value):
        try:
            with os.fdopen(
                os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600), "r+"
            ) as file:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                data = self._read(file)
                data[key] = value
                file.seek(0)
                json.dump(data, file)
                file.truncate()
        except OSError as e:
            logging.warning("Cannot write cache %s: %s", self.path, e)
