import unittest

from config import resolve
from config.errors import ConfigError


class TestResolve(unittest.TestCase):
    def test_env_beats_file(self):
        cfg = resolve(path="нет-такого.ini", env={"SERVICE_PORT": "9000"})
        self.assertEqual(cfg["port"], 9000)

    def test_missing_host_is_error(self):
        with self.assertRaises(ConfigError):
            resolve(path="нет-такого.ini", env={"SERVICE_HOST": ""})


if __name__ == "__main__":
    unittest.main()
