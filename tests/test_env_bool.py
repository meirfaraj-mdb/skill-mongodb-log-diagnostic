import os
import unittest
from unittest.mock import patch

from agent.env_bool import parse_bool_env


class ParseBoolEnvTests(unittest.TestCase):
    def test_unset_or_empty_uses_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(parse_bool_env("FLAG", default=False))
        with patch.dict(os.environ, {"FLAG": "  "}, clear=True):
            self.assertTrue(parse_bool_env("FLAG", default=True))

    def test_accepts_common_true_and_false_values(self):
        for value in ("true", "TRUE", "1", "yes", "on"):
            with self.subTest(value=value), patch.dict(os.environ, {"FLAG": value}, clear=True):
                self.assertTrue(parse_bool_env("FLAG", default=False))
        for value in ("false", "FALSE", "0", "no", "off"):
            with self.subTest(value=value), patch.dict(os.environ, {"FLAG": value}, clear=True):
                self.assertFalse(parse_bool_env("FLAG", default=True))

    def test_rejects_unrecognized_values_with_value_in_error(self):
        with patch.dict(os.environ, {"FLAG": "sometimes"}, clear=True):
            with self.assertRaisesRegex(ValueError, "sometimes"):
                parse_bool_env("FLAG", default=False)


if __name__ == "__main__":
    unittest.main()
