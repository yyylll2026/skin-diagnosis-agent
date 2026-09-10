"""知识库入库链路：解析 → 清洗 → 分块 → 向量化 → 落 Chroma + 登记元数据。

设计取舍说明：

1. **不用 LangChain Community 的几十种 DocumentLoader**，而是自己写轻量解析。
   原因是 community 的 loader 依赖面极广（unstructured、pdfminer、pillow……），
   本地部署装起来又慢又容易版本冲突；本项目只需 5 种格式，手写反而更可控。
   分块仍然使用 LangChain 的 ``RecursiveCharacterTextSplitter``（中文分隔符已适配），
   保持「LangChain 架构」的抽象一致性。

2. **doc_id = 文件内容 SHA1**，而不是 UUID。这样同一份文件重复上传天然幂等，
   文档更新后重新上传即可覆盖旧版本，不会在库里留下孤儿向量。

3. **Markdown 走两级切分**：先用 ``MarkdownHeaderTextSplitter`` 按标题切，
   把章节路径写进 metadata，再按字符递归切。这样检索时能回答「这个片段属于哪一节」，
   对医学文档尤其重要（同样的句子放在「鉴别诊断」和「护理建议」下含义完全不同）。
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from langchain_core.documents import Document
from langchain_text_splitters import (
    MarkdownHeaderTextSplitter,
    RecursiveCharacterTextSplitter,
)
from loguru import logger
from starlette.concurrency import run_in_threadpool

from app.config import Settings, get_settings
from app.core.errors import DocumentParseError
from app.repositories import vector_store
from app.repositories.db import MetadataStore
from app.schemas.knowledge import DocumentInfo, IngestResult

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".markdown", ".txt", ".html", ".htm"}

# 中文友好的递归分隔符：优先段落，其次中文句末标点，最后退到字符
CHINESE_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、", "：", " ", ""]

_MD_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3")]

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_BLANK = re.compile(r"\n{3,}")
# 只匹配「横向」空白（普通空格 / 制表符 / 全角空格），刻意排除换行，
# 以免把段落分隔也当成 PDF 噪声删掉
_CJK_INNER_SPACE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])")


# ---------------------------------------------------------------------------
# 文本清洗
# ---------------------------------------------------------------------------


def clean_text(text: str) -> str:
    """清洗文本：去控制字符、压掉多余空行、统一换行。

    Args:
        text: 原始文本。

    Returns:
        清洗后的文本。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_CHARS.sub(" ", text)
    # 修复 PDF 常见的「中文之间被塞了空格」。
    # 这里必须用 [ \t\u3000] 而不是 \s —— \s 包含 \n，
    # 会把汉字之间的换行也一并删掉，导致整篇文档的段落边界全部消失、
    # 分块退化成一大坨连续文本。这个坑很隐蔽，已用测试锁住。
    text = _CJK_INNER_SPACE.sub("", text)
    text = _MULTI_BLANK.sub("\n\n", text)
    return text.strip()


def _read_text_file(path: Path) -> str:
    """读取文本文件，带编码回退（utf-8 → gbk → 忽略错误）。"""
    for encoding in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="ignore")


# ---------------------------------------------------------------------------
# 各格式解析器
# ---------------------------------------------------------------------------


def _load_pdf(path: Path) -> list[Document]:
    """按页解析 PDF，保留页码便于溯源。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    docs: list[Document] = []
    for index, page in enumerate(reader.pages, start=1):
        try:
            text = page.extract_text() or ""
        except Exception as exc:  # noqa: BLE001 - 单页解析失败不该中断整篇
            logger.warning("PDF 第 {} 页解析失败: {}", index, exc)
            continue
        text = clean_text(text)
        if text:
            docs.append(Document(page_content=text, metadata={"page": index}))
    if not docs:
        raise DocumentParseError(f"PDF 未提取到可用文本（可能是纯扫描件）：{path.name}")
    return docs


def _load_docx(path: Path) -> list[Document]:
    """解析 Word：正文段落 + 表格（表格按行拍平成 ``a | b | c``）。"""
    import docx

    document = docx.Document(str(path))
    parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]

    for table in document.tables:
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            if any(cells):
                parts.append(" | ".join(cells))

    text = clean_text("\n".join(parts))
    if not text:
        raise DocumentParseError(f"Word 文档内容为空：{path.name}")
    return [Document(page_content=text, metadata={})]


def _load_html(path: Path) -> list[Document]:
    """解析 HTML：剔除脚本/样式后取正文，标题转成 Markdown 便于后续按标题切分。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_read_text_file(path), "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer"]):
        tag.decompose()

    blocks: list[str] = []
    for element in soup.find_all(["h1", "h2", "h3", "h4", "p", "li", "td"]):
        text = clean_text(element.get_text(" ", strip=True))
        if not text:
            continue
        if element.name in {"h1", "h2", "h3", "h4"}:
            blocks.append(f"{'#' * int(element.name[1])} {text}")
        else:
            blocks.append(text)

    text = clean_text("\n\n".join(blocks))
    if not text:
        raise DocumentParseError(f"HTML 未提取到正文：{path.name}")
    return [Document(page_content=text, metadata={})]


def _load_markdown(path: Path) -> list[Document]:
    """Markdown 直接读原文，保留 ``#`` 标题结构供后续切分使用。"""
    text = clean_text(_read_text_file(path))
    if not text:
        raise DocumentParseError(f"Markdown 内容为空：{path.name}")
    return [Document(page_content=text, metadata={})]


_LOADERS = {
    ".pdf": _load_pdf,
    ".docx": _load_docx,
    ".html": _load_html,
    ".htm": _load_html,
    ".md": _load_markdown,
    ".markdown": _load_markdown,
    ".txt": _load_markdown,
}


def load_file(path: Path) -> list[Document]:
    """解析单个文件为 LangChain Document 列表。

    Args:
        path: 文件路径。

    Returns:
        Document 列表（PDF 会一页一个）。

    Raises:
        DocumentParseError: 格式不支持或解析失败。
    """
    suffix = path.suffix.lower()
    loader = _LOADERS.get(suffix)
    if loader is None:
        raise DocumentParseError(
            f"不支持的文件格式 {suffix}，当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )
    try:
        return loader(path)
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(f"解析 {path.name} 失败：{exc}") from exc


# ---------------------------------------------------------------------------
# 分块
# ---------------------------------------------------------------------------


def split_documents(docs: Iterable[Document], settings: Settings) -> list[Document]:
    """把原始 Document 切成检索用的块。

    Markdown 走「标题切分 → 字符递归切分」两级；其它格式直接递归切分。
    每个块都会带上 ``section``（所在章节）与 ``chunk_index``（文内序号）。

    Args:
        docs: 原始文档列表。
        settings: 全局配置（提供 chunk_size / chunk_overlap）。

    Returns:
        分块后的 Document 列表。
    """
    recursive = RecursiveCharacterTextSplitter(
        chunk_size=settings.chunk_size,
        chunk_overlap=settings.chunk_overlap,
        separators=CHINESE_SEPARATORS,
        keep_separator="end",
        length_function=len,
    )
    header_splitter = MarkdownHeaderTextSplitter(
        headers_to_split_on=_MD_HEADERS, strip_headers=False
    )

    chunks: list[Document] = []
    for doc in docs:
        text = doc.page_content
        is_markdown = text.lstrip().startswith("#") or "\n## " in text

        if is_markdown:
            try:
                sections = header_splitter.split_text(text)
            except Exception as exc:  # noqa: BLE001 - 切分失败退回纯递归
                logger.warning("Markdown 标题切分失败，退回递归切分: {}", exc)
                sections = [Document(page_content=text, metadata={})]
            for section in sections:
                heading_path = " > ".join(
                    str(section.metadata[key])
                    for key in ("h1", "h2", "h3")
                    if section.metadata.get(key)
                )
                for piece in recursive.split_documents([section]):
                    piece.metadata.update(doc.metadata)
                    if heading_path:
                        piece.metadata["section"] = heading_path[:200]
                    chunks.append(piece)
        else:
            for piece in recursive.split_documents([doc]):
                piece.metadata.update(doc.metadata)
                chunks.append(piece)

    # 补块内序号，并丢掉过短的碎片（通常是页眉页脚残留）。
    # 这里不用 enumerate 的下标：被丢弃的碎片不该占用序号，否则序号会出现空洞
    result: list[Document] = []
    for chunk in chunks:
        content = chunk.page_content.strip()
        if len(content) < 20:
            continue
        chunk.page_content = content
        chunk.metadata["chunk_index"] = len(result)
        result.append(chunk)
    return result


# ---------------------------------------------------------------------------
# 入库服务
# ---------------------------------------------------------------------------


class IngestService:
    """知识库写入服务。"""

    def __init__(self, settings: Settings | None = None, store: MetadataStore | None = None):
        """初始化。

        Args:
            settings: 全局配置，默认取单例。
            store: 元数据仓储，默认按配置新建。
        """
        self.settings = settings or get_settings()
        self.store = store or MetadataStore(self.settings.resolved_sqlite_path)

    # ------------------------------------------------------------------
    @staticmethod
    def compute_doc_id(path: Path) -> str:
        """按文件内容计算 doc_id（SHA1 前 16 位）。

        用内容哈希而非 UUID：同一文件重复上传幂等，内容变更即视为新版本。
        """
        digest = hashlib.sha1(path.read_bytes()).hexdigest()
        return digest[:16]

    def _build_metadata(
        self, chunks: list[Document], doc_id: str, title: str, source: str, category: str
    ) -> list[Document]:
        """给每个块补齐统一的 metadata。"""
        now = datetime.now().isoformat(timespec="seconds")
        for chunk in chunks:
            content_hash = hashlib.sha1(chunk.page_content.encode("utf-8")).hexdigest()
            chunk.metadata.update(
                {
                    "doc_id": doc_id,
                    "title": title,
                    "source": source,
                    "category": category,
                    "ingest_time": now,
                    "content_hash": content_hash,
                    "chunk_id": f"{doc_id}::{chunk.metadata.get('chunk_index', 0)}",
                }
            )
        return chunks

    def _ingest_sync(self, path: Path, category: str, title: str | None) -> IngestResult:
        """同步入库流程（由线程池执行，避免阻塞事件循环）。"""
        started = time.perf_counter()
        doc_id = self.compute_doc_id(path)
        doc_title = title or path.stem
        source = path.name

        docs = load_file(path)
        chunks = split_documents(docs, self.settings)
        if not chunks:
            raise DocumentParseError(f"{path.name} 分块后为空，可能是内容过少或格式异常")

        chunks = self._build_metadata(chunks, doc_id, doc_title, source, category)

        # 先删后写：保证「重新上传同一文档」得到的是覆盖而非叠加
        removed = vector_store.delete_by_doc_id(doc_id)
        if removed:
            logger.info("覆盖旧版本 {}，清理 {} 个旧向量块", doc_id, removed)

        vectorstore = vector_store.get_vectorstore()
        ids = [str(chunk.metadata["chunk_id"]) for chunk in chunks]
        vectorstore.add_documents(documents=chunks, ids=ids)

        self.store.upsert_document(
            DocumentInfo(
                doc_id=doc_id,
                title=doc_title,
                source=source,
                category=category,
                chunk_count=len(chunks),
                size_bytes=path.stat().st_size,
                created_at=datetime.now().isoformat(timespec="seconds"),
            )
        )

        elapsed = int((time.perf_counter() - started) * 1000)
        logger.info("入库完成 {{doc_id={}}} {} 块，耗时 {}ms", doc_id, len(chunks), elapsed)

        return IngestResult(
            doc_id=doc_id,
            title=doc_title,
            source=source,
            category=category,
            chunk_count=len(chunks),
            elapsed_ms=elapsed,
            message=f"已入库 {len(chunks)} 个文本块",
        )

    async def ingest_file(
        self, path: Path, category: str = "其他", title: str | None = None
    ) -> IngestResult:
        """异步入库单个文件。

        Args:
            path: 文件路径。
            category: 文档分类，用于检索期元数据过滤。
            title: 自定义标题，默认取文件名。

        Returns:
            入库结果。

        Raises:
            DocumentParseError: 解析或分块失败。
        """
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            raise DocumentParseError(
                f"不支持的文件格式 {path.suffix}，当前支持：{', '.join(sorted(SUPPORTED_SUFFIXES))}"
            )

        result = await run_in_threadpool(self._ingest_sync, path, category, title)
        self._invalidate_retrieval_index()
        return result

    async def ingest_directory(
        self, directory: Path, category: str = "其他", recursive: bool = True
    ) -> list[IngestResult]:
        """批量入库目录下的全部支持的文档。

        Args:
            directory: 目录路径。
            category: 统一分类。
            recursive: 是否递归子目录。

        Returns:
            每个文件的入库结果；单个文件失败会被记录并跳过，不影响其它文件。
        """
        pattern = "**/*" if recursive else "*"
        files = sorted(
            p
            for p in directory.glob(pattern)
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        )
        results: list[IngestResult] = []
        for file_path in files:
            try:
                results.append(await self.ingest_file(file_path, category=category))
            except Exception as exc:  # noqa: BLE001 - 批量导入不因单文件失败而中断
                logger.error("跳过 {}：{}", file_path.name, exc)
        return results

    def delete_document(self, doc_id: str) -> bool:
        """删除文档：同时清理向量块与元数据登记。

        Args:
            doc_id: 文档 ID。

        Returns:
            是否命中并删除。
        """
        removed = vector_store.delete_by_doc_id(doc_id)
        hit = self.store.delete_document(doc_id)
        self._invalidate_retrieval_index()
        logger.info("删除文档 {}：清理 {} 个向量块", doc_id, removed)
        return hit

    @staticmethod
    def _invalidate_retrieval_index() -> None:
        """让 BM25 内存索引失效（向量库变更后必须重建关键词索引）。"""
        try:
            from app.services.retrieval_service import get_retrieval_service

            get_retrieval_service().invalidate()
        except Exception as exc:  # noqa: BLE001 - 索引失效失败不影响入库结果
            logger.warning("失效 BM25 索引失败: {}", exc)
