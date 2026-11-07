from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tsw.config import (
    CONFIG_FILE,
    DEFAULTS,
    LOCAL_FILE,
    load_config,
    merge,
    normalize_model_id,
    resolve_repository,
)
from tsw.errors import TswError
from tsw.home import Home

pytestmark = pytest.mark.use_case("UC-CONFIG-001")


def _write(home: Home, rel: str, value: Any) -> None:
    path = home.path(rel)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _problems(home: Home, config: dict[str, Any], local: dict[str, Any] | None = None) -> list[str]:
    _write(home, CONFIG_FILE, {"version": 1, **config})
    if local is not None:
        _write(home, LOCAL_FILE, local)
    return load_config(home).problems


def test_merge_objects_by_field_replaces_arrays_and_null_deletes() -> None:
    base = {"a": {"x": 1, "y": [1, 2]}, "b": 1}
    merged = merge(base, {"a": {"y": [3], "z": 2}, "b": None})
    assert merged == {"a": {"x": 1, "y": [3], "z": 2}}
    assert base == {"a": {"x": 1, "y": [1, 2]}, "b": 1}  # inputs are not modified


def test_defaults_alone_are_valid_and_derive_input_channels(home: Home) -> None:
    config = load_config(home)
    assert config.valid, config.problems
    assert config.get("input.channels") == ["in-session"]
    assert config.sources["input.channels"] == "derived"
    assert config.sources["roles.reviewer.model"] == "default"
    assert DEFAULTS["roles"]["reviewer"]["model"] == "gpt-6-astra"


def test_sources_record_which_layer_set_each_value(home: Home) -> None:
    _write(
        home,
        CONFIG_FILE,
        {"version": 1, "worktrees": {"root": "~/a"}, "scheduling": {"max_workers": 2}},
    )
    _write(home, LOCAL_FILE, {"worktrees": {"root": "~/b"}})
    config = load_config(home)
    assert config.valid, config.problems
    assert config.get("worktrees.root") == "~/b"
    assert config.sources["worktrees.root"] == "local"
    assert config.sources["scheduling.max_workers"] == "config"


def test_invalid_json_and_wrong_version(home: Home) -> None:
    home.path(CONFIG_FILE).write_text("{not json", encoding="utf-8")
    assert "不是合法的 JSON" in load_config(home).problems[0]
    assert "version: 必须为 1" in _problems(home, {"version": 2})


def test_unknown_fields_and_wrong_types_are_errors(home: Home) -> None:
    problems = _problems(home, {"bogus": 1, "scheduling": {"max_workers": "5", "x": 1}})
    assert "bogus: 未知字段" in problems
    assert "scheduling.max_workers: 应为整数" in problems
    assert "scheduling.x: 未知字段" in problems


def test_review_cannot_be_made_optional(home: Home) -> None:
    problems = _problems(home, {"review": {"required": False, "on_unavailable": "skip"}})
    assert "review.required: 评审是必经环节，不能配置" in problems
    assert "review.on_unavailable: 评审是必经环节，不能配置" in problems


def test_removed_comparison_role_is_only_a_warning(home: Home) -> None:
    _write(home, CONFIG_FILE, {"version": 1, "roles": {"comparison": {"model": "x"}}})
    config = load_config(home)
    assert config.valid
    assert config.warnings == ["roles.comparison: 已删除的字段，已忽略"]


def test_model_ids_and_efforts(home: Home) -> None:
    problems = _problems(home, {"roles": {"worker": {"model": "bad model"}}})
    assert problems == ["roles.worker.model: 模型 ID 格式不正确"]
    problems = _problems(home, {"roles": {"worker": {"effort": "huge"}}})
    assert any(p.startswith("roles.worker.effort: 取值应为") for p in problems)


@pytest.mark.parametrize(
    "config",
    [
        {"plugins": {"sessions": 42}},
        {"plugins": {"human_channels": [{}]}},
        {"input": 42},
        {"repositories": ["app"]},
        {"roles": {"reviewer": 7}},
    ],
)
def test_wrong_structure_is_reported_without_crashing(home: Home, config: dict[str, Any]) -> None:
    problems = _problems(home, config)
    assert problems
    assert not load_config(home).valid


@pytest.mark.parametrize(
    ("config", "problem"),
    [
        ({"ledger": {"checkpoint_every": None}}, "ledger.checkpoint_every: 缺少必填字段"),
        ({"scheduling": {"lease_minutes": None}}, "scheduling.lease_minutes: 缺少必填字段"),
        ({"roles": {"reviewer": None}}, "roles.reviewer.session: 缺少必填字段"),
        (
            {"roles": {"orchestrator": {"permission_mode": None}}},
            "roles.orchestrator.permission_mode: 缺少必填字段",
        ),
    ],
)
def test_required_fields_cannot_be_deleted(
    home: Home, config: dict[str, Any], problem: str
) -> None:
    assert problem in _problems(home, config)


def test_optional_fields_can_be_deleted(home: Home) -> None:
    assert not _problems(home, {"roles": {"reviewer": {"fallback_session": None}}})


def test_nested_null_deletes_inside_a_new_object(home: Home) -> None:
    assert merge({}, {"a": {"b": None, "c": 1}}) == {"a": {"c": 1}}
    assert merge({"a": 1}, {"a": {"b": None}}) == {"a": {}}
    assert not _problems(home, {"repositories": {"app": {"path": "~/a", "aliases": None}}})


def test_worker_permission_cannot_exceed_orchestrator(home: Home) -> None:
    problems = _problems(home, {"roles": {"worker": {"permission_mode": "full"}}})
    assert "roles.worker.permission_mode: 不能高于 roles.orchestrator.permission_mode" in problems
    assert not _problems(home, {"roles": {"worker": {"permission_mode": "edits"}}})


def test_session_roles_need_loaded_adapters_with_the_right_capabilities(home: Home) -> None:
    problems = _problems(
        home,
        {
            "plugins": {"sessions": ["claude-app", "copilot-app"]},
            "roles": {"reviewer": {"session": "claude-app", "fallback_session": "codex-exec"}},
        },
    )
    assert "plugins.sessions: 本版本不提供会话适配器 copilot-app" in problems
    assert any("claude-app 不能做评审器" in p for p in problems)
    assert (
        "roles.reviewer.fallback_session: 必须是 plugins.sessions 中已加载的会话适配器" in problems
    )
    problems = _problems(home, {"roles": {"worker": {"session": "codex-app-server"}}})
    assert any("不能承载 worker" in p for p in problems)


def test_reviewer_on_the_worker_model_is_a_warning(home: Home) -> None:
    _write(
        home,
        CONFIG_FILE,
        {"version": 1, "roles": {"reviewer": {"model": "claude-opus-5-5"}}},
    )
    config = load_config(home)
    assert config.valid
    assert any("评审视角的独立性下降" in w for w in config.warnings)


def test_channels_and_decision_digest(home: Home) -> None:
    problems = _problems(home, {"input": {"channels": ["teams-chat"]}})
    assert "input.channels: teams-chat 未在 plugins.human_channels 中加载" in problems
    problems = _problems(home, {"plugins": {"human_channels": ["teams-chat"]}})
    assert "plugins.human_channels: 本版本不提供人类渠道 teams-chat" in problems
    problems = _problems(home, {"input": {"decision_digest": {"channel": "in-session"}}})
    assert any("不是已加载且支持 notify 的渠道" in p for p in problems)
    problems = _problems(home, {"input": {"decision_digest": {"channel": None}}})
    assert "input.decision_digest.channel: 启用决定摘要时必须设置" in problems


def test_template_paths_must_exist(home: Home) -> None:
    problems = _problems(home, {"roles": {"worker": {"prompt_template": "config/prompts/w.md"}}})
    assert "roles.worker.prompt_template: 文件不存在 config/prompts/w.md" in problems
    home.path("config/prompts/w.md").write_text("模板", encoding="utf-8")
    assert not _problems(home, {"roles": {"worker": {"prompt_template": "config/prompts/w.md"}}})


def _repo(**overrides: Any) -> dict[str, Any]:
    repo: dict[str, Any] = {
        "path": "~/src/app",
        "provider": "github",
        "host": "github.com",
        "auth_profile": "gh",
    }
    repo.update(overrides)
    return repo


def test_repositories_accounts_and_aliases(home: Home, tmp_path: Path) -> None:
    accounts = {
        "gh": {
            "provider": "github",
            "host": "github.com",
            "auth": {"config_dir": str(tmp_path / "auth")},
        }
    }
    assert not _problems(
        home, {"repositories": {"app": _repo(aliases=["a"])}, "accounts": accounts}
    )
    problems = _problems(
        home,
        {
            "repositories": {
                "app": _repo(aliases=["x"], host="example.com"),
                "web": _repo(aliases=["x", "app"], auth_profile="nobody"),
            },
            "accounts": {"gh": {**accounts["gh"], "auth": {"config_dir": "auth/gh"}}},
        },
    )
    assert "repositories.app.host: 与账号 gh 的 host 不一致" in problems
    assert "repositories.web.auth_profile: accounts 中没有 nobody" in problems
    assert "repositories.web.aliases: x 已被 app 使用" in problems
    assert "repositories.web.aliases: app 已被 app 使用" in problems
    assert "accounts.gh.auth.config_dir: 凭据目录必须位于数据目录之外" in problems


def test_local_override_is_limited_to_machine_paths(home: Home) -> None:
    problems = _problems(
        home,
        {"repositories": {"app": {"path": "~/a"}}},
        local={"repositories": {"app": {"path": "~/b"}}, "roles": {"worker": {"model": "x"}}},
    )
    assert problems == [
        "config/task-steward.local.json: 不允许覆盖 roles.worker.model"
        "（只允许 ['repositories.*.path', 'worktrees.root', 'roles.reviewer.executable', "
        "'accounts.*.auth.config_dir', 'import.sources.*.root']）"
    ]


def test_require_valid_refuses_with_the_problems(home: Home) -> None:
    _write(home, CONFIG_FILE, {"version": 1, "bogus": True})
    with pytest.raises(TswError) as info:
        load_config(home).require_valid()
    assert info.value.details["reason"] == "config_invalid"
    assert info.value.details["problems"] == ["bogus: 未知字段"]


def test_resolve_repository_by_name_or_alias(home: Home) -> None:
    _write(home, CONFIG_FILE, {"version": 1, "repositories": {"example-app": {"aliases": ["app"]}}})
    config = load_config(home)
    assert resolve_repository(config, "example-app") == "example-app"
    assert resolve_repository(config, "app") == "example-app"
    assert resolve_repository(config, "other") is None


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [("claude-sonnet-5-5[1m]", "claude-sonnet-5-5"), ("gpt-6-astra", "gpt-6-astra")],
)
def test_model_ids_are_normalized_before_comparison(raw: str, normalized: str) -> None:
    assert normalize_model_id(raw) == normalized


@pytest.mark.use_case("UC-MERGE-001")
def test_a_merge_authorization_names_its_repository(home: Home) -> None:
    repo: dict[str, Any] = {"path": "~/src/demo", "delivery": {"push": True, "merge": True}}
    problems = _problems(home, {"version": 1, "repositories": {"demo": repo}})
    assert any("repositories.demo.repository" in p for p in problems)
    named = {**repo, "repository": "example/demo"}
    assert _problems(home, {"version": 1, "repositories": {"demo": named}}) == []
