"""知识库运维 CLI。

为什么需要一个 CLI 而不是只靠 HTTP 接口：
1. **批量导入**：几百份 PDF 走 HTTP 逐个上传太慢，CLI 可以直接读目录；
2. **更换 embedding 模型后的全量重建**：必须在服务启动前完成（否则维度冲突），
   这时候 HTTP 接口还不一定可用；
3. **离线排查**：检索效果不好时，直接在终端里 `search` 看命中了什么，
   比在前端点开抽屉面板快得多。

用法示例：
    python -m scripts.ingest_cli ingest ./data/docs --category 皮肤疾病
    python -m scripts.ingest_cli search "面部红斑伴鳞屑怎么鉴别"
    python -m scripts.ingest_cli list
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from app.config import get_settings
from app.core.errors import setup_logging
from app.repositories import vector_store
from app.repositories.db import MetadataStore
from app.services.ingest_service import IngestService
from app.services.retrieval_service import get_retrieval_service

app = typer.Typer(
    name="skin-kb",
    help="皮肤诊断智能体 · 知识库运维工具",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _store() -> MetadataStore:
    """构造元数据仓储。"""
    return MetadataStore(get_settings().resolved_sqlite_path)


@app.command("ingest")
def ingest(
    path: Annotated[Path, typer.Argument(help="文件或目录路径")],
    category: Annotated[str, typer.Option("--category", "-c", help="文档分类")] = "其他",
    recursive: Annotated[bool, typer.Option("--recursive/--no-recursive", help="是否递归子目录")] = True,
) -> None:
    """导入单个文件或整个目录到知识库。"""
    setup_logging(debug=False)
    settings = get_settings()
    service = IngestService(settings, _store())

    if not path.exists():
        console.print(f"[red]路径不存在：{path}[/red]")
        raise typer.Exit(code=1)

    async def run() -> None:
        if path.is_file():
            result = await service.ingest_file(path, category=category)
            results = [result]
        else:
            results = await service.ingest_directory(path, category=category, recursive=recursive)

        if not results:
            console.print("[yellow]未找到可导入的文档（支持 pdf/docx/md/txt/html）[/yellow]")
            return

        table = Table(title=f"入库完成 · 共 {len(results)} 份", show_lines=False)
        table.add_column("文档", style="cyan", no_wrap=True)
        table.add_column("分类")
        table.add_column("分块", justify="right")
        table.add_column("耗时(ms)", justify="right")
        for item in results:
            table.add_row(item.title, item.category, str(item.chunk_count), str(item.elapsed_ms))
        console.print(table)
        console.print(f"[green]当前集合向量块总数：{vector_store.count_chunks()}[/green]")

    asyncio.run(run())


@app.command("list")
def list_documents() -> None:
    """列出已入库文档。"""
    setup_logging(debug=False)
    documents = _store().list_documents()
    if not documents:
        console.print("[yellow]知识库为空[/yellow]")
        return

    table = Table(title=f"已入库文档 · {len(documents)} 份")
    table.add_column("doc_id", style="magenta", no_wrap=True)
    table.add_column("标题", style="cyan")
    table.add_column("来源")
    table.add_column("分类")
    table.add_column("分块", justify="right")
    for info in documents:
        table.add_row(info.doc_id, info.title, info.source, info.category, str(info.chunk_count))
    console.print(table)
    console.print(f"[green]向量块总数：{vector_store.count_chunks()}[/green]")


@app.command("delete")
def delete(
    doc_id: Annotated[str, typer.Argument(help="文档 ID（可用 list 查看）")],
) -> None:
    """按 doc_id 删除文档及其全部向量块。"""
    setup_logging(debug=False)
    settings = get_settings()
    service = IngestService(settings, _store())
    if service.delete_document(doc_id):
        console.print(f"[green]已删除 {doc_id}[/green]")
    else:
        console.print(f"[yellow]未找到 {doc_id}[/yellow]")
        raise typer.Exit(code=1)


@app.command("search")
def search(
    query: Annotated[str, typer.Argument(help="检索语句")],
    top_k: Annotated[int, typer.Option("--top-k", "-k", help="返回条数")] = 5,
    category: Annotated[str | None, typer.Option("--category", "-c", help="按分类过滤")] = None,
) -> None:
    """直接测试混合检索链路（不调用 LLM，秒级返回）。"""
    setup_logging(debug=False)
    results = get_retrieval_service().search(query, top_k=top_k, category=category)

    if not results:
        console.print("[yellow]未命中任何片段[/yellow]")
        return

    for index, item in enumerate(results, start=1):
        header = f"[{index}] {item.title}（{item.source}）｜相关度 {item.score:.1%}"
        console.print(Panel(item.snippet, title=header, border_style="cyan", expand=True))


@app.command("reindex")
def reindex(
    docs_dir: Annotated[Path | None, typer.Option("--docs", help="语料目录，默认 data/docs")] = None,
    category: Annotated[str, typer.Option("--category", "-c")] = "其他",
) -> None:
    """清空向量库并从语料目录全量重建（更换 embedding 模型后必须执行）。"""
    setup_logging(debug=False)
    settings = get_settings()
    target = docs_dir or settings.docs_dir
    service = IngestService(settings, _store())

    console.print(f"[yellow]即将清空集合 {settings.chroma_collection} 并重建[/yellow]")
    vector_store.reset_collection()

    async def run() -> None:
        results = await service.ingest_directory(target, category=category)
        console.print(f"[green]重建完成：{len(results)} 份文档，"
                      f"{sum(item.chunk_count for item in results)} 个向量块[/green]")

    asyncio.run(run())


@app.command("health")
def health() -> None:
    """检查 Ollama 连通性与模型就绪情况。"""
    setup_logging(debug=False)
    import httpx

    settings = get_settings()
    console.print(f"Ollama: [cyan]{settings.ollama_api}[/cyan]")
    try:
        resp = httpx.get(f"{settings.ollama_api}/api/tags", timeout=5.0)
        resp.raise_for_status()
        names = {m.get("name", "") for m in resp.json().get("models", [])}
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]连接失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    def mark(model: str) -> str:
        base = model.split(":")[0]
        hit = model in names or any(name.split(":")[0] == base for name in names)
        return f"[green]{model}[/green]" if hit else f"[red]{model}（未拉取）[/red]"

    console.print(f"视觉模型: {mark(settings.vision_model)}")
    console.print(f"文本模型: {mark(settings.text_model)}")
    console.print(f"轻量模型: {mark(settings.fast_model)}")
    if settings.embedding_backend == "ollama":
        console.print(f"向量模型: {mark(settings.embedding_model)}")
    else:
        console.print(f"向量模型: {settings.embedding_backend} / {settings.embedding_model}")

    document_count = len(_store().list_documents())
    console.print(
        f"知识库: [cyan]{vector_store.count_chunks()}[/cyan] 个向量块"
        f"，{document_count} 份文档"
    )


if __name__ == "__main__":
    app()
