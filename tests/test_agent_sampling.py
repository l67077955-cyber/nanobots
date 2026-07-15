from nanobot.groupchat.runtime.round import _valid_agent_sampling
from nanobot.groupchat.runtime.tools.tool_chat import valid_agent_sampling


def test_valid_agent_sampling_filters_unknown_keys():
    raw = {
        "temperature": 0.5,
        "top_p": 1.0,
        "reasoning_effort": "low",
        "trump": 1.0,
    }

    assert valid_agent_sampling(raw) == {
        "temperature": 0.5,
        "top_p": 1.0,
        "reasoning_effort": "low",
    }


def test_broadcast_agent_sampling_uses_same_filter():
    cfg = {
        "hyperparams": {
            "temperature": 0.1,
            "min_p": 0.07,
            "not_a_provider_param": True,
        }
    }

    assert _valid_agent_sampling(cfg) == {
        "temperature": 0.1,
        "min_p": 0.07,
    }
