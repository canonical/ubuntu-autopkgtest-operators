import fcntl
import json
from pathlib import Path


class KeyValueCache:
    def __init__(self, cache_path):
        self.path = Path(cache_path)
        if not self.path.exists():
            with open(self.path, "w") as f:
                json.dump({}, f)

    def _lock(self, file, mode):
        if mode == "r":
            fcntl.flock(file, fcntl.LOCK_SH)
        if mode == "w":
            fcntl.flock(file, fcntl.LOCK_EX)

    def _unlock(self, file):
        fcntl.flock(file, fcntl.LOCK_UN)

    def _write(self, data, file):
        file.seek(0)
        json.dump(data, file, default=str)
        file.truncate()

    def get(self, key):
        with open(self.path, "r") as f:
            self._lock(f, "r")
            try:
                data = json.load(f)
                return data.get(key, None)
            finally:
                self._unlock(f)

    def set(self, key, value):
        with open(self.path, "r+") as f:
            self._lock(f, "w")
            try:
                data = json.load(f)
                data[key] = value
                self._write(data, f)
            finally:
                self._unlock(f)

    def delete(self, key):
        with open(self.path, "r+") as f:
            self._lock(f, "w")
            try:
                data = json.load(f)
                if key in data:
                    del data[key]
                    self._write(data, f)
            finally:
                self._unlock(f)

    def clear(self):
        with open(self.path, "w") as f:
            json.dump({}, f)
