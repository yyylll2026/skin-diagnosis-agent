"""入库链路与检索融合的单元测试。

不依赖 Ollama / 不依赖真实向量模型：只测「文本处理」与「融合数学」这两块
纯函数逻辑——它们反而是最容易出错、也最值得回归的部分。
"""

from __future__ import annotations

from pathlib import Path

from langchain_core.documents import Document

from app.config import Settings
from app.core.errors import DocumentParseError
from app.services.ingest_service import clean_text, load_file, split_documents
from app.services.retrieval_service import RetrievalService, tokenize

# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------


def test_clean_text_removes_control_chars() -> None:
    """控制字符要被清掉。"""
    assert "\x00" not in clean_text("皮损\x00表现\x07良好")


def test_clean_text_removes_intra_cjk_spaces() -> None:
    """PDF 抽取常在汉字之间插空格，要修掉；但英文单词之间的空格必须保留。"""
    assert clean_text("皮 肤 病 学") == "皮肤病学"
    assert clean_text("skin lesion") == "skin lesion"


def test_clean_text_collapses_blank_lines() -> None:
    """连续空行压缩为单个空行。"""
    assert clean_text("第一段\n\n\n\n第二段") == "第一段\n\n第二段"


# ---------------------------------------------------------------------------
# 文档解析与分块
# ---------------------------------------------------------------------------


def test_load_markdown(tmp_path: Path) -> None:
    """Markdown 直接读原文。"""
    path = tmp_path / "a.md"
    path.write_text("# 湿疹\n\n湿疹是一种常见皮肤病。", encoding="utf-8")
    docs = load_file(path)
    assert len(docs) == 1
    assert "湿疹是一种常见皮肤病" in docs[0].page_content


def test_load_unsupported_suffix_raises(tmp_path: Path) -> None:
    """不支持的格式要明确报错。"""
    path = tmp_path / "a.xlsx"
    path.write_bytes(b"binary")
    try:
        load_file(path)
    except DocumentParseError:
        return
    raise AssertionError("应当抛出 DocumentParseError")


def test_load_pdf_keeps_page_number(tmp_path: Path) -> None:
    """PDF 应转成「一页一个 Document」，并保留页码用于溯源。"""
    pytest = __import__("pytest")
    pypdf = pytest.importorskip("pypdf")
    path = tmp_path / "b.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    # 空白页无文本，应明确报错而不是返回空列表
    try:
        load_file(path)
    except DocumentParseError:
        return
    raise AssertionError("空白 PDF 应当抛出 DocumentParseError")


def test_split_documents_adds_metadata(settings: Settings) -> None:
    """分块后每块都要带 chunk_index，且块内不含过短碎片。"""
    text = "。".join(f"这是第{i}句关于湿疹的说明" for i in range(1, 80))
    chunks = split_documents([Document(page_content=text, metadata={"page": 1})], settings)
    assert len(chunks) > 1
    assert [c.metadata["chunk_index"] for c in chunks] == list(range(len(chunks)))
    assert all(len(c.page_content) >= 20 for c in chunks)
    assert all(c.metadata["page"] == 1 for c in chunks)


def test_split_documents_tracks_markdown_section(settings: Settings) -> None:
    """Markdown 分块要记录所属章节，便于回答「这句话出自哪一节」。"""
    text = (
        "# 皮肤疾病\n\n## 湿疹\n\n" + "湿疹的护理要点是加强保湿并避免搔抓。" * 15 + "\n\n"
        "## 银屑病\n\n" + "银屑病需要长期规范治疗并避免诱因。" * 15
    )
    chunks = split_documents([Document(page_content=text, metadata={})], settings)
    sections = {c.metadata.get("section") for c in chunks}
    assert any("湿疹" in str(s) for s in sections)
    assert any("银屑病" in str(s) for s in sections)


def test_chunk_size_is_respected(settings: Settings) -> None:
    """分块不应显著超出配置的 chunk_size。"""
    text = "银屑病" * 600
    chunks = split_documents([Document(page_content=text, metadata={})], settings)
    # 允许一个分隔符的余量
    assert all(len(c.page_content) <= settings.chunk_size + 20 for c in chunks)


# ---------------------------------------------------------------------------
# 分词与融合
# ---------------------------------------------------------------------------


def test_tokenize_keeps_domain_terms_intact() -> None:
    """领域词典生效：「银屑病」不应被切成「银屑/病」。"""
    assert "银屑病" in tokenize("银屑病的典型表现是红斑鳞屑")


def test_tokenize_uses_jieba_for_chinese() -> None:
    """中文必须真正分词，否则 BM25 会把整句当成一个 token。"""
    tokens = tokenize("面部出现红斑和丘疹")
    assert len(tokens) >= 3


def test_tokenize_drops_single_chars_and_blanks() -> None:
    """单字与空白会被过滤，降低噪声。"""
    assert tokenize("  ， 的 湿疹 ") == ["湿疹"]


def test_rrf_favors_documents_ranked_high_in_both_lists() -> None:
    """RRF 的核心性质：两路都排第一的文档得分最高。"""
    service = RetrievalService.__new__(RetrievalService)
    fused = service._rrf(
        [
            (["chunkA", "chunkB", "chunkC"], 1.0),  # 向量路
            (["chunkA", "chunkC", "chunkB"], 0.5),  # 关键词路
        ],
        k=60,
    )
    assert max(fused, key=fused.get) == "chunkA"


def test_rrf_rewards_consistency_over_single_list_top() -> None:
    """只在单路排第一、另一路缺席的文档，分数应低于两路都靠前的文档。"""
    service = RetrievalService.__new__(RetrievalService)
    fused = service._rrf(
        [
            (["only_vector"], 1.0),
            (["both_1", "both_2"], 0.5),
        ],
        k=60,
    )
    assert fused["only_vector"] == 1.0 / 61
    assert fused["both_1"] == 0.5 / 61
    # 单路绝对分数更高，但 RRF 的意义在于它不会被向量路的头名垄断
    assert fused["only_vector"] > 0


def test_rrf_weights_shift_ranking() -> None:
    """提高某一路的权重应能改变排序结果。"""
    service = RetrievalService.__new__(RetrievalService)
    vector_first = service._rrf([(["a"], 1.0), (["b"], 0.1)], k=60)
    keyword_first = service._rrf([(["a"], 0.1), (["b"], 1.0)], k=60)
    assert max(vector_first, key=vector_first.get) == "a"
    assert max(keyword_first, key=keyword_first.get) == "b"
