"""Group chat configuration schema."""

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class Base(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class GroupChatAgentConfig(Base):
    """Configuration for a single group chat agent."""

    model: str = "anthropic/claude-sonnet-4-5"
    persona: str = ""  # Path to SOUL.md or inline persona text
    character_json: str = ""  # Path to SillyTavern character.json (fallback)


class GroupChatConfig(Base):
    """Configuration for multi-agent group chat."""

    enabled: bool = False
    mode: str = "broadcast"  # "serial" | "broadcast"
    agents_dir: str = ""  # Path to agents directory (e.g. "agents/")
    agents: dict[str, GroupChatAgentConfig] = Field(default_factory=dict)
    excluded_agents: list[str] = Field(default_factory=list)
    max_rounds: int = 30
    max_history: int = 50
    auto_reply_delay: int = 0  # seconds between agent turns
    max_tokens: int = 3000  # max tokens per agent response (match SillyTavern)
    idle_wait_timeout: int = 0  # seconds to wait for messages after work (0 = no auto-rest, stays alive until MAX_CYCLES)

