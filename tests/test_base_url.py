"""The LM Studio base URL, whichever of its URLs was pasted in."""

import pytest

from controller_inbox.config import Settings


@pytest.mark.parametrize(
    "given",
    [
        "http://localhost:1234/v1",
        "http://localhost:1234/v1/",
        "http://localhost:1234/v1/chat/completions",
        "http://localhost:1234/api/v1/chat",
        "http://localhost:1234/api/v1/models",
        "http://localhost:1234",
        "localhost:1234",
        " http://localhost:1234/v1/models ",
    ],
)
def test_any_lm_studio_url_becomes_the_v1_base(given):
    assert Settings(llm_base_url=given, _env_file=None).llm_base_url == "http://localhost:1234/v1"


@pytest.mark.parametrize(
    "given, expected",
    [
        ("http://192.168.1.20:1234/api/v1/chat", "http://192.168.1.20:1234/v1"),
        ("http://10.0.0.5:1234/api/v0/models", "http://10.0.0.5:1234/v1"),
        ("http://[::1]:1234/api/v1", "http://[::1]:1234/v1"),
        ("http://studio-pc:1234/api/v1/chat", "http://studio-pc:1234/v1"),
        ("http://studio-pc.local:1234/api/v1", "http://studio-pc.local:1234/v1"),
    ],
)
def test_lm_studio_on_another_computer_nearby(given, expected):
    assert Settings(llm_base_url=given, _env_file=None).llm_base_url == expected


@pytest.mark.parametrize(
    "given",
    [
        "https://openrouter.ai/api/v1",
        "https://openrouter.ai/api/v1/",
        "https://openrouter.ai/api/v1/chat/completions",
    ],
)
def test_hosted_gateways_under_api_keep_it(given):
    assert Settings(llm_base_url=given, _env_file=None).llm_base_url == "https://openrouter.ai/api/v1"


def test_other_servers_keep_their_path():
    assert Settings(llm_base_url="http://127.0.0.1:11434/v1", _env_file=None).llm_base_url == "http://127.0.0.1:11434/v1"
    assert Settings(llm_base_url="https://gateway.example/openai/v1", _env_file=None).llm_base_url == "https://gateway.example/openai/v1"


@pytest.mark.parametrize(
    "given, expected",
    [
        ("http://127.0.0.1:1234/v1/embeddings", "http://127.0.0.1:1234/v1"),
        ("127.0.0.1:1234", "http://127.0.0.1:1234/v1"),
        ("http://127.0.0.1:1234/api/v0", "http://127.0.0.1:1234/v1"),
        ("", ""),
        ("  ", ""),
    ],
)
def test_the_embedding_url_is_read_like_the_chat_url(given, expected):
    # empty still means "use the chat server's URL"
    assert Settings(embedding_base_url=given, _env_file=None).embedding_base_url == expected
