"""注册表变更预演接口 POST /api/v1/registry/packages/{name}/preview 测试。"""

import json

from app import main as main_module
from app.solver import SolveResult


def test_preview_new_version_changes_resolution(client):
    # 新增 core-lib 1.6.0：cachekit@1.3.0 要求 ^1.4.0，解从 1.5.0 变为 1.6.0
    resp = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={
            "versions": [{"version": "1.6.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "cachekit", "range": "~1.3.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["package"] == "core-lib"
    assert body["versions"] == ["1.6.0"]

    assert body["current"]["status"] == "feasible"
    assert body["current"]["resolution"]["core-lib"] == "1.5.0"
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"]["core-lib"] == "1.6.0"
    assert body["current"]["conflicts"] == []

    changes = body["changes"]
    assert changes is not None
    assert changes["status_changed"] is False
    assert changes["changed"] == ["core-lib"]
    assert changes["unchanged"] == ["cachekit"]
    assert changes["added"] == [] and changes["removed"] == []
    version_change = next(v for v in changes["version_changes"] if v["package"] == "core-lib")
    assert version_change["before"] == "1.5.0"
    assert version_change["after"] == "1.6.0"
    assert version_change["change"] == "changed"


def test_preview_upsert_semantics_keeps_unlisted_versions(client):
    # PUT 是逐版本 upsert：未列出的 1.5.0 等必须保留；这里只覆盖 2.0.0 的依赖
    resp = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={
            "versions": [{"version": "2.0.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "core-lib", "range": "~1.5.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 1.5.0 未被写入删除，两侧都应可行且选中 1.5.0
    assert body["current"]["resolution"]["core-lib"] == "1.5.0"
    assert body["preview"]["resolution"]["core-lib"] == "1.5.0"
    assert body["changes"]["unchanged"] == ["core-lib"]


def test_preview_dependency_override_can_flip_feasible_to_infeasible(client):
    # 覆盖 cachekit 1.3.0：其依赖改为 core-lib ^2.0.0，与清单另一约束冲突
    resp = client.post(
        "/api/v1/registry/packages/cachekit/preview",
        json={
            "versions": [
                {"version": "1.3.0", "dependencies": {"core-lib": "^2.0.0"}}
            ],
            "manifest": {
                "dependencies": [
                    {"name": "cachekit", "range": "~1.3.0"},
                    {"name": "core-lib", "range": "~1.5.0"},
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # 当前 registry 中 cachekit 1.3.0 -> core-lib ^1.4.0 与 ~1.5.0 可同时满足
    assert body["current"]["status"] == "feasible"
    assert body["current"]["resolution"]["core-lib"] == "1.5.0"
    assert body["preview"]["status"] == "infeasible"
    assert body["preview"]["resolution"] is None
    assert body["preview"]["conflicts"]
    assert body["preview"]["conflicts"][0]["package"] == "core-lib"

    changes = body["changes"]
    assert changes["status_changed"] is True
    # feasible -> infeasible：预演无可行解，当前解中的包均视为 removed
    assert set(changes["removed"]) == {"cachekit", "core-lib"}
    assert changes["added"] == [] and changes["changed"] == [] and changes["unchanged"] == []


def test_preview_infeasible_to_feasible_when_adding_satisfying_version(client):
    # 只加 loggerkit 0.9.0 与 cachekit 1.3.0 当前即冲突；
    # 改为预演一个不依赖 core-lib 的 cachekit 新版本来消解冲突
    resp = client.post(
        "/api/v1/registry/packages/cachekit/preview",
        json={
            "versions": [{"version": "1.4.0", "dependencies": {}}],
            "manifest": {
                "dependencies": [
                    {"name": "cachekit", "range": "^1.0.0"},
                    {"name": "loggerkit", "range": "~0.9.0"},
                ]
            },
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "infeasible"
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"]["cachekit"] == "1.4.0"
    assert body["preview"]["resolution"]["core-lib"] == "2.1.0"
    changes = body["changes"]
    assert changes["status_changed"] is True
    assert set(changes["added"]) == {"cachekit", "core-lib", "loggerkit"}


def test_preview_does_not_write_registry_or_audits(client):
    before = client.get("/api/v1/registry/packages").json()
    resp = client.post(
        "/api/v1/registry/packages/newpkg/preview",
        json={
            "versions": [{"version": "3.0.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "newpkg", "range": "^3.0.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "infeasible"
    assert body["current"]["conflicts"][0]["kind"] == "package_missing"
    assert body["preview"]["status"] == "feasible"
    assert body["preview"]["resolution"] == {"newpkg": "3.0.0"}

    # 注册表未写入
    after = client.get("/api/v1/registry/packages").json()
    assert after == before
    assert "newpkg" not in after["packages"]
    # 不产生审计记录
    assert client.get("/api/v1/audits").json()["total"] == 0


def test_preview_invalid_inputs_return_422(client):
    base_manifest = {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]}

    # 非法版本号
    r = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={"versions": [{"version": "not-semver", "dependencies": {}}],
              "manifest": base_manifest},
    )
    assert r.status_code == 422

    # 非法依赖范围
    r = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={"versions": [{"version": "1.0.0", "dependencies": {"x": "bad-range"}}],
              "manifest": base_manifest},
    )
    assert r.status_code == 422

    # 空版本列表
    r = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={"versions": [], "manifest": base_manifest},
    )
    assert r.status_code == 422

    # 清单非法范围
    r = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={"versions": [{"version": "1.0.0", "dependencies": {}}],
              "manifest": {"dependencies": [{"name": "x", "range": "bad"}]}},
    )
    assert r.status_code == 422

    # 根插件版本在注册表中不存在
    r = client.post(
        "/api/v1/registry/packages/other/preview",
        json={"versions": [{"version": "1.0.0", "dependencies": {}}],
              "manifest": {"plugin_name": "megaplugin", "plugin_version": "9.9.9",
                           "dependencies": []}},
    )
    assert r.status_code == 422

    # 根插件只提供一半（plugin_name 无 plugin_version）
    r = client.post(
        "/api/v1/registry/packages/other/preview",
        json={"versions": [{"version": "1.0.0", "dependencies": {}}],
              "manifest": {"plugin_name": "megaplugin", "dependencies": []}},
    )
    assert r.status_code == 422

    # 路径包名非法
    r = client.post(
        "/api/v1/registry/packages/%20/preview",
        json={"versions": [{"version": "1.0.0", "dependencies": {}}],
              "manifest": base_manifest},
    )
    assert r.status_code == 422


def test_preview_root_plugin_resolves_on_both_sides(client):
    resp = client.post(
        "/api/v1/registry/packages/authkit/preview",
        json={
            "versions": [
                {"version": "3.0.0",
                 "dependencies": {"core-lib": "^2.0.0", "cachekit": "^1.0.0"}}
            ],
            "manifest": {"plugin_name": "megaplugin", "plugin_version": "1.0.0",
                         "dependencies": []},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "feasible"
    assert body["preview"]["status"] == "feasible"
    # 预演不影响根插件的传递解析（authkit 被 ^1.0.0 约束，3.0.0 不满足）
    assert body["preview"]["resolution"]["authkit"] == "1.2.0"
    assert body["changes"]["status_changed"] is False


class _BudgetZeroSolver(main_module.Solver):
    """搜索预算为 0 的求解器：首次选节点即 inconclusive。"""

    def __init__(self, registry, node_budget=None):
        super().__init__(registry, node_budget=0)


def test_preview_inconclusive_on_both_sides_has_null_changes(client, monkeypatch):
    monkeypatch.setattr(main_module, "Solver", _BudgetZeroSolver)
    resp = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={
            "versions": [{"version": "1.6.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "core-lib", "range": "^1.0.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "inconclusive"
    assert body["preview"]["status"] == "inconclusive"
    assert body["current"]["resolution"] is None
    assert body["changes"] is None


def test_preview_inconclusive_on_one_side_has_null_changes(client, monkeypatch):
    real_solve = main_module.Solver.solve

    def patched_solve(self, root_dependencies, root_name=None, root_version=None):
        # 仅预演注册表（包含 core-lib 1.6.0）一侧耗尽预算
        core_versions = {str(pv.version) for pv in self.registry.get("core-lib", [])}
        if "1.6.0" in core_versions:
            return SolveResult(
                status="inconclusive",
                node_count=999,
                explanation="预算耗尽（测试注入）",
            )
        return real_solve(self, root_dependencies, root_name=root_name,
                          root_version=root_version)

    monkeypatch.setattr(main_module.Solver, "solve", patched_solve)
    resp = client.post(
        "/api/v1/registry/packages/core-lib/preview",
        json={
            "versions": [{"version": "1.6.0", "dependencies": {}}],
            "manifest": {"dependencies": [{"name": "cachekit", "range": "~1.3.0"}]},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["current"]["status"] == "feasible"
    assert body["preview"]["status"] == "inconclusive"
    # 任一侧 inconclusive 都不得推断变化
    assert body["changes"] is None


def test_preview_index_lists_endpoint(client):
    endpoints = client.get("/").json()["endpoints"]
    assert "POST /api/v1/registry/packages/{name}/preview" in endpoints


def test_preview_repeated_calls_are_idempotent_reads(client):
    payload = {
        "versions": [{"version": "1.6.0", "dependencies": {}}],
        "manifest": {"dependencies": [{"name": "cachekit", "range": "~1.3.0"}]},
    }
    first = client.post("/api/v1/registry/packages/core-lib/preview", json=payload).json()
    second = client.post("/api/v1/registry/packages/core-lib/preview", json=payload).json()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
