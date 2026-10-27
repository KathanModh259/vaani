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

    def test_blank_values_fall_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("DOCS_ENABLED", "")
        monkeypatch.setenv("DB_POOL_SIZE", "")
        monkeypatch.setenv("RATE_LIMIT_ENABLED", "  ")
        s = Settings()
        assert s.DB_POOL_SIZE == 10 and s.RATE_LIMIT_ENABLED is True

    def test_debug_from_env(self):
        os.environ["DEBUG"] = "true"
        try:
            settings = Settings()
            assert settings.DEBUG is True
        finally:
            os.environ.pop("DEBUG", None)

    def test_public_origin_comes_from_the_platform_when_not_configured(self, monkeypatch):
        """Render assigns the hostname, so the blueprint must not hard-code it."""
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
        monkeypatch.setenv("RENDER_EXTERNAL_URL", "https://vaani-x1y2.onrender.com/")
        monkeypatch.setenv("CORS_ORIGINS", "")
        s = Settings()
        assert s.PUBLIC_BASE_URL == "https://vaani-x1y2.onrender.com"
        assert s.CORS_ORIGINS == ["https://vaani-x1y2.onrender.com"]
