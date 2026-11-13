import os
import unittest
from unittest.mock import patch

from app.config import Settings


class TestSecretKeyPathConfiguration(unittest.TestCase):
    def test_reads_secret_key_path_from_environment(self):
        with patch.dict(os.environ, {"DEX_SECRET_KEY_PATH": "/var/lib/liquidity/secret.key"}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(
            settings.secret_key_path,
            "/var/lib/liquidity/secret.key",
        )

    def test_defaults_secret_key_path_when_environment_variable_is_missing(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.secret_key_path, "")


if __name__ == "__main__":
    unittest.main()
