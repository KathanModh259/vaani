import pytest
import os
from scripts.config import Settings


class TestSettings:
    def test_default_values(self):
        os.environ.pop("HOST", None)
        os.environ.pop("PORT", None)
        os.environ.pop("DATABASE_URL", None)
        try:
            settings = Settings()
            assert settings.HOST == "127.0.0.1"
            assert settings.PORT == 8000
            assert settings.DEBUG is False
            assert "sqlite" in settings.DATABASE_URL
        finally:
            os.environ.pop("HOST", None)
            os.environ.pop("PORT", None)
            os.environ.pop("DATABASE_URL", None)

    def test_debug_from_env(self):
        os.environ["DEBUG"] = "true"
        try:
            settings = Settings()
            assert settings.DEBUG is True
        finally:
            os.environ.pop("DEBUG", None)
