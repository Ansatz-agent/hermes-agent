from tools.memory_tool import check_memory_requirements


def test_builtin_memory_tool_hidden_when_both_markdown_stores_disabled(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "memory": {
                "memory_enabled": False,
                "user_profile_enabled": False,
                "provider": "profile_memory",
            }
        },
    )
    assert check_memory_requirements() is False


def test_builtin_memory_tool_available_when_user_profile_markdown_enabled(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {
            "memory": {
                "memory_enabled": False,
                "user_profile_enabled": True,
            }
        },
    )
    assert check_memory_requirements() is True
