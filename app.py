"""Small Gradio reader for the MinerU paper library."""
from __future__ import annotations

import sys
from pathlib import Path

import gradio as gr

sys.path.insert(0, str(Path(__file__).parent))

from config.settings import MINERU_BACKEND, MINERU_EFFORT, PAPERS_DIR
from src.catalog_folders.reader import create_safe_catalog_reader
from src.services.paper_library import PaperLibrary
from src.prompt_builder import PromptBuilder


# Lazy-initialized services — set to None at import so tests can monkeypatch
# before any handler runs, and so ``import app`` never accesses real catalog.
catalog = None
library = None
prompt_builder = None


def _ensure_services():
    """Lazily initialize catalog, library, and prompt_builder on first use."""
    global catalog, library, prompt_builder
    if catalog is None:
        catalog = create_safe_catalog_reader()
    if library is None:
        library = PaperLibrary(papers_dir=PAPERS_DIR)
    if prompt_builder is None:
        prompt_builder = PromptBuilder(catalog=catalog, library=library)


def list_papers():
    _ensure_services()
    try:
        papers = catalog.list_papers()
    except RuntimeError as exc:
        return f"文献索引尚未发布或校验失败：{exc}"
    if not papers:
        return "文献库为空。请先通过 paper_raw CLI 完成入库。"
    lines = ["| # | paper_number | paper_name | title |", "|---|---|---|---|"]
    for i, item in enumerate(papers, 1):
        content_identity = item.get("content_identity") or {}
        title = content_identity.get("content_title_zh") or ""
        lines.append(f"| {i} | `{item.get('paper_number','')}` | `{item.get('paper_name','')}` | {title} |")
    return "\n".join(lines)


def view_markdown(identifier):
    _ensure_services()
    value = identifier.strip()
    if not value:
        return "请输入 paper_number 或 paper_name"
    try:
        text = library.read_markdown(value, max_chars=8000)
    except Exception as exc:
        return f"读取失败: {exc}"
    return text or "未找到正式 Markdown"


def gen_catalog_entry_prompt(paper_name):
    _ensure_services()
    pid = paper_name.strip()
    if not pid:
        return "请输入 paper_name"
    out = prompt_builder.build_catalog_entry_prompt(pid)
    return out["prompt"] if out.get("success") else f"失败: {out.get('error')}"


def gen_plan_prompt(question):
    _ensure_services()
    q = question.strip()
    if not q:
        return "请输入研究问题"
    out = prompt_builder.build_catalog_planning_prompt(q)
    return out["prompt"] if out.get("success") else f"失败: {out.get('error')}"


def gen_fulltext_prompt(question, paper_names_text):
    _ensure_services()
    q = question.strip()
    ids = [s.strip() for s in paper_names_text.split(",") if s.strip()]
    if not q or not ids:
        return "请输入研究问题和 paper_name 列表"
    out = prompt_builder.build_fulltext_prompt(q, ids)
    return out["prompt"] if out.get("success") else f"失败: {out.get('error')}"


def get_status():
    _ensure_services()
    try:
        count=len(catalog.list_papers()); index_state="已发布"
    except (RuntimeError, FileNotFoundError, ValueError):
        count=0; index_state="未发布/校验失败"
    return f"""## 系统状态
| 项目 | 状态 |
|---|---|
| 模式 | current_metadata_catalog |
| 文献数量 | {count} |
| 索引 generation | {index_state} |
| MinerU 后端 | {MINERU_BACKEND} |
| Effort | {MINERU_EFFORT} |
"""


THEME = gr.themes.Soft(primary_hue="indigo", neutral_hue="slate")

with gr.Blocks(title="MinerU 文献资产库") as app:
    gr.Markdown("# MinerU 文献资产库\n正式入库只通过 paper_raw CLI 完成；本界面只读正式资产并生成 prompt。")
    with gr.Tabs():
        with gr.TabItem("文献库"):
            refresh_btn = gr.Button("刷新")
            doc_list = gr.Markdown()
            refresh_btn.click(list_papers, outputs=[doc_list])
        with gr.TabItem("全文阅读"):
            pid_input = gr.Textbox(label="paper_number 或 paper_name")
            view_btn = gr.Button("查看全文")
            md_out = gr.Textbox(label="Markdown 前 8000 字符", lines=24)
            view_btn.click(view_markdown, inputs=[pid_input], outputs=[md_out])
        with gr.TabItem("Prompt"):
            entry_pid = gr.Textbox(label="catalog-entry paper_name")
            entry_btn = gr.Button("生成 catalog-entry prompt")
            entry_out = gr.Textbox(label="Prompt", lines=16)
            entry_btn.click(gen_catalog_entry_prompt, inputs=[entry_pid], outputs=[entry_out])
            plan_q = gr.Textbox(label="研究问题")
            plan_btn = gr.Button("生成目录规划 prompt")
            plan_out = gr.Textbox(label="Prompt", lines=16)
            plan_btn.click(gen_plan_prompt, inputs=[plan_q], outputs=[plan_out])
            full_q = gr.Textbox(label="研究问题")
            full_ids = gr.Textbox(label="paper_name 列表，逗号分隔")
            full_btn = gr.Button("生成全文写作 prompt")
            full_out = gr.Textbox(label="Prompt", lines=16)
            full_btn.click(gen_fulltext_prompt, inputs=[full_q, full_ids], outputs=[full_out])
        with gr.TabItem("状态"):
            status_out = gr.Markdown(value="## 系统状态\n加载中...")
            gr.Button("刷新").click(get_status, outputs=[status_out])
    app.load(list_papers, outputs=[doc_list])
    app.load(get_status, outputs=[status_out])


if __name__ == "__main__":
    app.launch(server_name="127.0.0.1", server_port=7860, share=False, show_error=True, theme=THEME)
