"""Model settings stored locally; the API key is protected by Windows DPAPI."""

from __future__ import annotations

from novelforge_windows.domain.models import ModelSettings
from novelforge_windows.infrastructure.repositories import SettingsRepository
from novelforge_windows.infrastructure.secrets import protect_secret, unprotect_secret


class ModelSettingsService:
    API_KEY_SETTING = "llm_api_key_dpapi"

    def __init__(self, settings: SettingsRepository) -> None:
        self.settings = settings

    def load(self) -> ModelSettings:
        encrypted_key = self.settings.get(self.API_KEY_SETTING)
        return ModelSettings(
            base_url=self.settings.get("llm_base_url", "https://api.openai.com/v1"),
            model=self.settings.get("llm_model"),
            api_key=unprotect_secret(encrypted_key) if encrypted_key else "",
            temperature=float(self.settings.get("llm_temperature", "0.8")),
        )

    def save(self, model_settings: ModelSettings) -> ModelSettings:
        base_url = model_settings.base_url.strip().rstrip("/")
        if not base_url:
            raise ValueError("模型 Base URL 不能为空。")
        if not 0 <= model_settings.temperature <= 2:
            raise ValueError("Temperature 必须在 0 到 2 之间。")
        self.settings.set("llm_base_url", base_url)
        self.settings.set("llm_model", model_settings.model.strip())
        self.settings.set("llm_temperature", str(model_settings.temperature))
        if model_settings.api_key:
            self.settings.set(
                self.API_KEY_SETTING,
                protect_secret(model_settings.api_key.strip()),
            )
        else:
            self.settings.delete(self.API_KEY_SETTING)
        return self.load()

