"""LLM provider abstraction module."""

from nanobot.providers.base import LLMProvider, LLMResponse
from nanobot.providers.httpx_provider import HttpxProvider
from nanobot.providers.openai_codex_provider import OpenAICodexProvider
from nanobot.providers.azure_openai_provider import AzureOpenAIProvider

# Backward compatibility alias
LiteLLMProvider = HttpxProvider

__all__ = ["LLMProvider", "LLMResponse", "LiteLLMProvider", "HttpxProvider", "OpenAICodexProvider", "AzureOpenAIProvider"]
