from __future__ import annotations

from pydantic_settings import BaseSettings

PROVIDER_BASE_URLS: dict[str, str] = {
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://localhost:1234/v1",
    "anthropic": "https://api.anthropic.com/v1/",
}


class Settings(BaseSettings):
    database_url: str = "postgresql://postgres:level3@localhost:5432/level3"
    llm_provider: str = "anthropic"
    llm_model: str = "claude-sonnet-4-5-20250929"
    llm_api_key: str = ""
    llm_base_url: str = ""
    heartbeat_interval: int = 300
    max_conversation_history: int = 100
    max_tool_iterations: int = 30

    model_config = {"env_file": ".env"}

    def get_base_url(self) -> str:
        if self.llm_base_url:
            return self.llm_base_url
        return PROVIDER_BASE_URLS.get(self.llm_provider, "")

    def get_api_key(self) -> str:
        if self.llm_api_key:
            return self.llm_api_key
        if self.llm_provider in ("ollama", "lmstudio"):
            return "not-needed"
        return ""
