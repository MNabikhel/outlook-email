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


def test_other_servers_keep_their_path():
    assert Settings(llm_base_url="http://127.0.0.1:11434/v1", _env_file=None).llm_base_url == "http://127.0.0.1:11434/v1"
    assert Settings(llm_base_url="https://gateway.example/openai/v1", _env_file=None).llm_base_url == "https://gateway.example/openai/v1"
