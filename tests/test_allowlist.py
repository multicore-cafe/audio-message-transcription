"""Parsing and matching rules of the allowed-models file."""

import pytest

from transcription_bot.config import ConfigError
from transcription_bot.mcp_server.allowlist import (
    REFUSAL,
    AllowList,
    access_policy,
    allowed_models_path,
    load_allowlist,
)


def test_exact_wildcard_and_slash_model_ids():
    allow = AllowList.parse("# comment\noneprovider/my-model\nsomeprovider/team/turbo\notherprovider/*\n")
    assert allow.entries == ("oneprovider/my-model", "someprovider/team/turbo", "otherprovider/*")
    assert allow.models == "oneprovider/my-model, someprovider/team/turbo, otherprovider/*"
    # Exact match, including a model id that itself contains `/`.
    assert allow.is_allowed("oneprovider", "my-model")
    assert allow.is_allowed("someprovider", "team/turbo")
    # The wildcard matches every model of that provider, and nothing else.
    assert allow.is_allowed("otherprovider", "anything")
    assert allow.is_allowed("otherprovider", "team/other")
    # No access for an unlisted model, an unlisted provider, or an unknown model.
    assert not allow.is_allowed("someprovider", "team/other")
    assert not allow.is_allowed("cloudprovider", "kimi")
    assert not allow.is_allowed("", "x")
    assert not allow.is_allowed(None, None)


def test_blank_lines_comments_and_duplicates():
    allow = AllowList.parse("\n  \n# a comment\noneprovider/my-model\n oneprovider/my-model \n")
    assert allow.entries == ("oneprovider/my-model",)


@pytest.mark.parametrize("line", [
    "no-slash\n",
    "provider/\n",
    "/model\n",
    "provider / model\n",
    "provider/model extra\n",
    "*/model\n",
])
def test_invalid_lines_are_rejected(line):
    with pytest.raises(ConfigError, match="expected"):
        AllowList.parse(line)


def test_an_empty_or_comment_only_file_allows_nothing():
    for text in ("", "\n\n", "# just a comment\n"):
        with pytest.raises(ConfigError, match="no allowed models configured"):
            AllowList.parse(text)


def test_load_reads_the_file_and_names_it_on_failure(tmp_path):
    path = tmp_path / "allowed-models"
    path.write_text("oneprovider/my-model\n", encoding="utf-8")
    assert load_allowlist(path).is_allowed("oneprovider", "my-model")
    missing = tmp_path / "nope"
    with pytest.raises(ConfigError, match=str(missing)):
        load_allowlist(missing)
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match=str(empty)):
        load_allowlist(empty)


def test_allowed_models_path_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_ALLOWED_MODELS_FILE", str(tmp_path / "mcp"))
    assert allowed_models_path() == tmp_path / "mcp"
    monkeypatch.delenv("MCP_ALLOWED_MODELS_FILE")
    monkeypatch.setenv("TELEGRAM_MCP_ALLOWED_MODELS_FILE", str(tmp_path / "pi"))
    assert allowed_models_path() == tmp_path / "pi"


def test_generated_policy_names_the_list_and_the_refusal():
    allow = AllowList.parse("oneprovider/my-model\notherprovider/*\n")
    policy = access_policy(allow)
    assert REFUSAL in policy
    assert "oneprovider/my-model, otherprovider/*" in policy
    assert "every other model must refuse" in policy
