"""仓储层全量演练。

**为什么单独抽一个模块**：切换数据库（SQLite → PostgreSQL）这类改造，
风险不在某一个方法，而在「有没有哪个方法的 SQL 没被验证到」。
把每个方法都真实调一遍，才能：

1. 让「记录全部 SQL 并检查方言可移植性」的测试覆盖到每一条语句——
   漏调一个方法，就有一条语句从验证里溜走；
2. 当作换库后的冒烟测试：所有方法都能正常读写，才算真的兼容。

刻意覆盖了带边界的分支（无关键词的模糊搜索、带关键词的、无归属的会话等），
因为它们走的是不同的 SQL。
"""

from __future__ import annotations

from app.repositories.db import MetadataStore
from app.schemas.knowledge import DocumentInfo

NOW = "2026-10-03T10:00:00"
LATER = "2026-10-04T10:00:00"


def run_repository_workout(store: MetadataStore) -> None:
    """把 :class:`MetadataStore` 的每个公开方法都调一遍。"""
    _documents(store)
    _sessions(store)
    _health_records(store)
    _analysis_tasks(store)
    _clarifications(store)
    _users(store)
    _auth_sessions(store)
    _audit_logs(store)


def _documents(store: MetadataStore) -> None:
    store.upsert_document(
        DocumentInfo(
            doc_id="doc-1",
            title="银屑病诊疗指南",
            source="psoriasis.md",
            category="皮肤疾病",
            chunk_count=12,
            size_bytes=2048,
            created_at=NOW,
        )
    )
    # 同一 doc_id 再写一次：走 ON CONFLICT 分支
    store.upsert_document(
        DocumentInfo(
            doc_id="doc-1",
            title="银屑病诊疗指南（修订）",
            source="psoriasis.md",
            category="皮肤疾病",
            chunk_count=14,
            size_bytes=2112,
            created_at=LATER,
        )
    )
    assert len(store.list_documents()) == 1
    assert store.get_document("doc-1") is not None
    assert store.get_document("doc-missing") is None
    assert store.delete_document("doc-missing") is False
    assert store.delete_document("doc-1") is True


def _sessions(store: MetadataStore) -> None:
    store.ensure_session("s-1", "第一段对话", "u-1")
    store.ensure_session("s-1", "改名不该生效", "u-2")  # 冲突分支：归属不覆盖
    store.ensure_session("s-anon")

    store.add_message("s-1", "user", "面部红斑", has_image=True)
    store.add_message("s-1", "assistant", "请补充持续时间")

    assert store.get_session_owner("s-1") == "u-1"
    assert store.get_session_owner("s-missing") is None
    assert len(store.list_sessions("u-1")) == 1
    assert len(store.list_sessions()) >= 2  # 管理员视角
    assert len(store.list_messages("s-1")) == 2

    store.delete_session("s-anon")


def _health_records(store: MetadataStore) -> None:
    record = {
        "record_id": "r-1",
        "user_id": "u-1",
        "recorded_on": "2026-10-01",
        "body_site": "左侧面颊",
        "symptoms": "红斑伴轻度瘙痒",
        "severity": "mild",
        "notes": "夜间加重",
        "created_at": NOW,
        "updated_at": NOW,
    }
    store.create_health_record(record)

    assert len(store.list_health_records("u-1")) == 1
    assert len(store.list_health_records("u-other")) == 0
    assert len(store.export_health_records("u-1")) == 1
    assert store.get_health_record("r-1", "u-1") is not None
    assert store.get_health_record("r-1", "u-other") is None
    assert store.list_health_records_since("u-1", "2026-09-01") != []
    assert store.list_health_records_since("u-1", "2026-11-01") == []
    assert store.count_health_records("u-1") == 1

    assert store.update_health_record("r-1", "u-1", {"severity": "moderate"}, LATER) is True
    # 没有可更新字段时走「只判存在」的分支
    assert store.update_health_record("r-1", "u-1", {"unknown_field": 1}, LATER) is True
    assert store.update_health_record("r-missing", "u-1", {"severity": "mild"}, LATER) is False

    store.delete_user_health_records("u-nobody")
    assert store.delete_health_record("r-1", "u-1") is True


def _analysis_tasks(store: MetadataStore) -> None:
    store.create_analysis_task(
        {
            "task_id": "t-1",
            "user_id": "u-1",
            "session_id": "s-1",
            "status": "queued",
            "progress": 0,
            "current_step": "排队中",
            "created_at": NOW,
            "updated_at": NOW,
            "started_at": "",
            "finished_at": "",
        }
    )

    assert store.get_analysis_task("t-1", "u-1") is not None
    assert store.get_analysis_task("t-1", "u-other") is None
    assert len(store.list_analysis_tasks("u-1")) == 1

    assert (
        store.update_analysis_task(
            "t-1",
            {
                "status": "running",
                "progress": 40,
                "current_step": "检索知识库",
                "result": {"answer": "占位"},
                "updated_at": LATER,
                "started_at": LATER,
            },
        )
        is True
    )
    # 空更新走「直接返回 False」的分支
    assert store.update_analysis_task("t-1", {"unknown_field": 1}) is False

    assert store.recover_interrupted_analysis_tasks(LATER) == 1  # 覆盖 MIN/LEAST 分支
    assert store.delete_analysis_tasks_for_session("s-1", "u-1") == 1

    store.create_analysis_task(
        {
            "task_id": "t-2",
            "user_id": "u-1",
            "session_id": "s-2",
            "status": "failed",
            "progress": 100,
            "current_step": "失败",
            "created_at": NOW,
            "updated_at": NOW,
            "started_at": NOW,
            "finished_at": NOW,
        }
    )
    assert store.delete_analysis_tasks_for_user("u-1") == 1


def _clarifications(store: MetadataStore) -> None:
    store.upsert_clarification("s-1", '{"duration": "三天"}', '["trigger"]', 1, NOW)
    assert store.get_clarification("s-1") is not None
    # 冲突分支：同 session 再写一次
    store.upsert_clarification("s-1", '{"duration": "五天"}', "[]", 2, LATER)
    assert store.get_clarification("s-1")["rounds"] == 2
    assert store.get_clarification("s-missing") is None

    # 被外部工具改坏的 JSON 要能回落而不是抛异常
    store.upsert_clarification("s-broken", "{不是 JSON", "也不是", 1, NOW)
    broken = store.get_clarification("s-broken")
    assert broken["collected"] == {}
    assert broken["pending_fields"] == []

    store.delete_clarification("s-1")


def _users(store: MetadataStore) -> None:
    store.create_user("u-1", "alice", "hash-1", "admin", "爱丽丝", NOW)
    store.create_user("u-2", "bob", "hash-2", "customer", "鲍勃", LATER)

    assert store.get_user("u-1") is not None
    assert store.get_user("u-missing") is None
    assert store.get_user_by_username("bob") is not None
    assert store.get_user_by_username("nobody") is None
    assert len(store.list_users()) == 2
    assert len(store.list_users("ali")) == 1  # 覆盖 LIKE/ILIKE 分支
    assert len(store.list_users("鲍")) == 1
    assert store.count_active_admins() == 1
    assert store.count_active_admins(exclude_user_id="u-1") == 0

    assert store.update_user("u-2", {"display_name": "Bob"}) is True
    assert store.update_user("u-2", {"unknown_field": 1}) is False
    assert store.update_user("u-missing", {"display_name": "x"}) is False

    store.delete_user("u-nobody")
    assert store.delete_user("u-2") is True


def _auth_sessions(store: MetadataStore) -> None:
    store.create_auth_session("token-a", "u-1", NOW, LATER, "pytest", "127.0.0.1")
    store.create_auth_session("token-old", "u-1", "2020-01-01T00:00:00", "2020-01-02T00:00:00")

    assert store.get_auth_session("token-a") is not None
    assert store.get_auth_session("token-missing") is None
    assert len(store.list_user_auth_sessions("u-1")) == 2

    store.touch_auth_session("token-a", LATER, LATER)
    assert store.purge_expired_auth_sessions(NOW) == 1
    assert store.delete_auth_session("token-missing") is False
    assert store.delete_auth_session("token-a") is True
    assert store.delete_user_auth_sessions("u-1") == 0


def _audit_logs(store: MetadataStore) -> None:
    store.add_audit_log("login", actor="alice", target="alice", created_at=NOW)
    store.add_audit_log(
        "kb.upload",
        actor="alice",
        target="doc-1",
        result="ok",
        detail="上传文档",
        client_ip="127.0.0.1",
        created_at=LATER,
    )

    assert len(store.list_audit_logs()) == 2
    assert len(store.list_audit_logs(action="login")) == 1
    assert store.list_audit_logs(action="nothing") == []


__all__ = ["run_repository_workout"]
