import unittest

from scripts.validate_online_shared_buffers import parse_mb


class OnlineSharedBuffersValidationTest(unittest.TestCase):
    def test_parse_memory_units(self):
        self.assertEqual(parse_mb("4096MB"), 4096)
        self.assertEqual(parse_mb("4GB"), 4096)
        self.assertEqual(parse_mb("524288"), 4096)

    def test_parse_rejects_invalid_value(self):
        with self.assertRaises(ValueError):
            parse_mb("not-a-memory-value")
