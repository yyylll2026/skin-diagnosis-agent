"""接口层测试。

刻意不进入 lifespan（不用 ``with TestClient(app)``）：预热会去建 Chroma 集合、
编译状态图，测试里没有这个必要，也会让首次运行变慢。
只验证「路由注册、响应契约、异常语义」这些接口层自己的职责。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
API = "/api/v1"


def test_service_info_is_reachable_in_both_modes() -> None:
    """服务信息必须可达，且要同时兼容两种部署模式。

    - **纯 API 模式**（没有 ``backend/web``）：``/`` 返回服务信息 JSON
    - **单容器模式**（有前端构建产物）：``/`` 返回前端页面，
      服务信息挪到 ``/service-info``

    之所以写成「两选一」的断言而不是写死某一种：这个差异取决于构建产物是否
    存在，把测试跟构建状态绑死会在「本地跑测试」和「容器里跑测试」时表现不一致。
    """
    root = client.get("/")
    assert root.status_code == 200

    if root.headers.get("content-type", "").startswith("application/json"):
        body = root.json()
    else:
        info = client.get("/service-info")
        assert info.status_code == 200, "托管前端时 /service-info 必须可达"
        body = info.json()

    assert body["api_prefix"] == API
    assert "disclaimer" in body
    assert body["models"]["vision"]


def test_api_routes_not_shadowed_by_static_mount() -> None:
    """回归测试：挂载静态资源到 "/" 之后，API 路由不能被吞掉。

    踩过的坑：``app.mount("/", StaticFiles(...))`` 会接管所有未匹配路径，
    如果注册顺序不对，``/service-info`` 这类自定义路由会 404
    （StaticFiles 会去找同名文件）。Starlette 按注册顺序匹配，
    所以静态挂载必须放在最后。
    """
    for path in ["/api/v1/system/health", "/docs", "/openapi.json"]:
        assert client.get(path).status_code == 200, f"{path} 被静态挂载吞掉了"


def test_openapi_schema_is_generated() -> None:
    """OpenAPI 文档能正常生成（校验所有路由的类型注解可用）。"""
    response = client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for route in [
        f"{API}/agent/chat",
        f"{API}/chat/qa",
        f"{API}/diagnosis/image",
        f"{API}/kb/documents",
        f"{API}/kb/search",
        f"{API}/system/health",
    ]:
        assert route in paths, f"缺少路由 {route}"


def test_health_is_always_200() -> None:
    """健康检查即使依赖不可用也必须返回 200，把问题写进 warnings。

    这是刻意的设计：它是「诊断面板」而不是「存活探针」，
    若返回 5xx，前端只会显示「服务异常」，用户看不到可操作的修复提示。
    """
    response = client.get(f"{API}/system/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] in {"ok", "degraded"}
    assert isinstance(body["warnings"], list)
    assert "embedding_backend" in body


def test_health_reports_model_readiness_flags() -> None:
    """模型就绪标志位必须存在，前端据此点亮状态灯。"""
    body = client.get(f"{API}/system/health").json()
    for key in [
        "ollama_connected",
        "vision_model_ready",
        "text_model_ready",
        "embedding_model_ready",
        "indexed_chunks",
        "indexed_documents",
    ]:
        assert key in body


def test_graph_endpoint_exports_mermaid() -> None:
    """/system/graph 导出的拓扑应包含全部节点名。"""
    response = client.get(f"{API}/system/graph")
    assert response.status_code == 200
    mermaid = response.json()["mermaid"]
    if not mermaid:  # 未安装 mermaid 渲染依赖时允许为空
        return
    for node in ["preprocess", "route", "vision_analyze", "retrieve", "generate", "safety_review"]:
        assert node in mermaid


def test_list_documents_returns_empty_list_initially() -> None:
    """空库返回空数组而不是 404。"""
    response = client.get(f"{API}/kb/documents")
    assert response.status_code == 200
    assert isinstance(response.json(), list)


def test_delete_missing_document_returns_404() -> None:
    """删除不存在的文档返回 404 且带可读信息。"""
    response = client.delete(f"{API}/kb/documents/not-exist")
    assert response.status_code == 404
    assert "不存在" in response.json()["detail"]


def test_reset_requires_explicit_confirmation() -> None:
    """清空知识库必须显式确认，防止误触。"""
    response = client.post(f"{API}/kb/reset")
    assert response.status_code == 400
    assert "confirm" in response.json()["detail"]


def test_validation_error_shape() -> None:
    """参数校验失败返回 400 且 message 指出出错位置。"""
    response = client.post(f"{API}/kb/search", json={"query": "", "top_k": 5})
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "validation_error"
    assert "query" in body["message"]


def test_session_history_of_unknown_session_is_empty() -> None:
    """查询不存在的会话返回空历史而不是报错。"""
    response = client.get(f"{API}/sessions/does-not-exist")
    assert response.status_code == 200
    assert response.json()["messages"] == []


def test_response_carries_timing_header() -> None:
    """所有响应都带服务端耗时头，便于联调定位慢点。"""
    response = client.get("/")
    assert "X-Elapsed-Ms" in response.headers


# ---------------------------------------------------------------------------
# 追问澄清：端到端（不依赖 Ollama）
#
# 这条链路在本轮请求内只经过 preprocess → route → check_completeness → clarify
# → safety_review，**没有任何模型调用**（判定与文案都是确定性规则），
# 所以可以在单测里完整跑通，包括免责声明的追加与多轮进度的持久化。
# ---------------------------------------------------------------------------


def test_clarify_flow_returns_structured_followup_without_llm() -> None:
    """信息不足时返回结构化追问，而不是硬着头皮作答。"""
    session = "s-e2e-clarify-1"
    response = client.post(
        f"{API}/chat/qa",
        json={"session_id": session, "message": "我脸上起了红疹", "stream": False},
    )
    assert response.status_code == 200
    body = response.json()

    clarify = body["clarify"]
    assert clarify["needs_clarification"] is True
    assert clarify["round"] == 1
    assert "出现时间" in clarify["missing"]
    assert clarify["collected"].get("部位与范围") == "脸"

    # 追问文本要结构化：逐条列出缺失维度并说明为什么问
    assert "出现时间" in body["answer"]
    assert "为什么要问" in body["answer"]
    # 没有检索、没有生成（因为压根没进入回答流程）
    assert body["sources"] == []
    # 但免责声明照常追加——追问分支同样经过 safety_review
    assert "不能替代" in body["answer"]


def test_clarify_flow_advances_to_second_round() -> None:
    """第二轮仍未补齐时继续追问，且轮次递增、已收集信息累积。"""
    session = "s-e2e-clarify-2"
    client.post(
        f"{API}/chat/qa",
        json={"session_id": session, "message": "我脸上起了红疹", "stream": False},
    )
    response = client.post(
        f"{API}/chat/qa",
        json={"session_id": session, "message": "感觉不太舒服", "stream": False},
    )
    assert response.status_code == 200
    clarify = response.json()["clarify"]

    assert clarify["needs_clarification"] is True
    assert clarify["round"] == 2
    # 上一轮收集到的部位要延续下来
    assert clarify["collected"].get("部位与范围") == "脸"


def test_clarify_user_skip_falls_back_instead_of_asking_again() -> None:
    """用户说「说不清 / 直接回答」时不再追问，转为兜底作答。"""
    session = "s-e2e-clarify-3"
    client.post(
        f"{API}/chat/qa",
        json={"session_id": session, "message": "我脸上起了红疹", "stream": False},
    )
    response = client.post(
        f"{API}/chat/qa",
        json={"session_id": session, "message": "说不清，直接回答吧", "stream": False},
    )
    assert response.status_code == 200
    clarify = response.json()["clarify"]
    assert clarify["needs_clarification"] is False
    assert clarify["fallback"] is True
    assert clarify["bypassed_reason"] == "user_skip"


def test_urgent_case_never_gets_clarified() -> None:
    """急症不追问——这是追问机制最重要的安全约束。

    注意危险信号会让请求继续走检索与生成，因此这里只断言
    「没有发起追问」，不断言答案内容（那取决于模型）。
    """
    session = "s-e2e-clarify-urgent"
    response = client.post(
        f"{API}/chat/qa",
        json={"session_id": session, "message": "我这颗痣迅速增大还出血", "stream": False},
    )
    assert response.status_code == 200
    clarify = response.json()["clarify"]
    assert clarify["needs_clarification"] is False
    assert clarify["bypassed_reason"] == "urgent"
