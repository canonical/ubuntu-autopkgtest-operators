"""Test the best-effort key-value cache."""

import os
import tempfile
from unittest import TestCase

from helpers.cache import KeyValueCache


class KeyValueCacheTests(TestCase):
    """Test KeyValueCache."""

    def test_corrupt_file(self):
        """Corrupt cache is a miss, and the next set() repairs it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "cache.json")
            with open(path, "w") as f:
                f.write("garbage")
            cache = KeyValueCache(path)

            with self.assertLogs(level="WARNING") as logs:
                self.assertIsNone(cache.get("key"))
                cache.set("key", 42)
            self.assertIn(path, logs.output[-1])
            self.assertEqual(cache.get("key"), 42)
