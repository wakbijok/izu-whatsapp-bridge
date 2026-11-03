"""In-process MCP tools for a guest lane: make a document and send it on
WhatsApp. No shell, no code execution: the model names a file and gives
Markdown; the bridge converts (LibreOffice) and sends it through the node
bridge, keeping a copy under the lane's folder for later edits."""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import markdown
from claude_agent_sdk import create_sdk_mcp_server, tool

from claude_wa import image_gen
from claude_wa.office_render import render_pptx, render_xlsx

log = logging.getLogger("claude-wa.docs")

FORMATS = ("docx", "pdf", "xlsx", "pptx", "md", "txt", "html")
SOFFICE = shutil.which("soffice") or shutil.which("libreoffice")
CSS = ("body{font-family:Calibri,Arial,sans-serif;font-size:11pt;line-height:1.4}"
       "h1{font-size:18pt}h2{font-size:14pt}h3{font-size:12pt}table{border-collapse:collapse}"
       "td,th{border:1px solid #999;padding:4px 8px}")


def safe_name(name: str, ext: str) -> str:
    base = Path((name or "").replace("\\", "/")).name  # no directories
    base = re.sub(r"[^\w.\- ]+", "", base).strip(" .")
    base = (Path(base).stem if base else "")[:80] or "dokumen"
    return f"{base}.{ext}"


def _inside(path: Path, root: Path) -> bool:
    try:
        p = path.resolve()
    except OSError:
        return False
    return p == root or root in p.parents


def render(markdown_text: str, fmt: str, out: Path) -> Path:
    """Write `markdown_text` as `fmt` to `out` (path with the right suffix)."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt in ("md", "txt"):
        out.write_text(markdown_text, encoding="utf-8")
        return out
    if fmt == "xlsx":
        return render_xlsx(markdown_text, out)
    if fmt == "pptx":
        return render_pptx(markdown_text, out)
    html = markdown.markdown(markdown_text, extensions=["tables", "sane_lists"])
    doc = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{html}</body></html>"
    if fmt == "html":
        out.write_text(doc, encoding="utf-8")
        return out
    if not SOFFICE:
        raise RuntimeError("LibreOffice is not installed on this host")
    with tempfile.TemporaryDirectory(prefix="docs-") as tmp:
        src = Path(tmp) / (out.stem + ".html")
        src.write_text(doc, encoding="utf-8")
        # Own profile dir per call: no lock fights with other conversions.
        prof = Path(tmp) / "profile"
        filt = "docx:MS Word 2007 XML" if fmt == "docx" else "pdf:writer_web_pdf_Export"
        r = subprocess.run(
            [SOFFICE, "--headless", "--norestore", f"-env:UserInstallation=file://{prof}",
             "--convert-to", filt, "--outdir", tmp, str(src)],
            capture_output=True, text=True, timeout=120,
        )
        produced = Path(tmp) / f"{out.stem}.{fmt}"
        if r.returncode != 0 or not produced.exists():
            raise RuntimeError(f"conversion failed: {(r.stderr or r.stdout)[-300:]}")
        shutil.move(str(produced), str(out))
    return out


def make_docs_server(root: Path, chat_id: str, send_media, *, images: bool = False,
                     read_roots: tuple[str, ...] = (), codex_home: Path | None = None):  # noqa: ANN001
    """SDK MCP server bound to one lane: files live under `root`, sends go to
    `chat_id` via `send_media(chat_id, path, caption)`. `codex_home`: the
    image run's own CODEX_HOME (outside `root`; default image_gen.DEFAULT_HOME)."""
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    roots = tuple(Path(r).expanduser().resolve() for r in read_roots) + (root,)

    def _ok(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}]}

    def _err(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": True}

    @tool(
        "create_document",
        "Create a file from Markdown and send it to the user as a WhatsApp attachment. "
        "format: docx (default, Word), pdf, xlsx (Excel: each Markdown table becomes a sheet, "
        "a heading before it names the sheet, numbers stay numbers), pptx (PowerPoint: each "
        "heading starts a slide, the lines under it are its bullets; the first heading alone "
        "is the title slide), md, txt. The file is also kept in your folder so you can edit it "
        "later by creating it again with the same filename. Use real Markdown (headings, "
        "lists, tables) — it becomes proper formatting.",
        {"filename": str, "content_markdown": str, "format": str, "caption": str},
    )
    async def create_document(args: dict) -> dict:
        fmt = (args.get("format") or "docx").lower().lstrip(".")
        if fmt not in FORMATS:
            return _err(f"format must be one of {', '.join(FORMATS)}")
        name = safe_name(args.get("filename") or "", fmt)
        out = root / name
        try:
            await asyncio.to_thread(render, args.get("content_markdown") or "", fmt, out)
        except Exception as e:  # noqa: BLE001
            log.warning("create_document failed: %s", e)
            return _err(f"could not create the document: {e}")
        try:
            await send_media(chat_id, str(out), (args.get("caption") or "").strip() or None)
        except Exception as e:  # noqa: BLE001
            return _err(f"document saved as {name} but sending failed: {e}")
        return _ok(f"sent {name} ({out.stat().st_size} bytes) and kept a copy in your folder")

    @tool(
        "send_file",
        "Send a file from your folder to the user as a WhatsApp attachment (e.g. a file "
        "they sent earlier, or one you created).",
        {"filename": str, "caption": str},
    )
    async def send_file(args: dict) -> dict:
        p = (root / (args.get("filename") or "")).resolve()
        if not _inside(p, root) or not p.is_file():
            return _err("no such file in your folder")
        try:
            await send_media(chat_id, str(p), (args.get("caption") or "").strip() or None)
        except Exception as e:  # noqa: BLE001
            return _err(f"sending failed: {e}")
        return _ok(f"sent {p.name}")

    @tool("list_files", "List the files in your folder.", {})
    async def list_files(args: dict) -> dict:  # noqa: ARG001
        files = sorted(p.name for p in root.iterdir() if p.is_file())
        return _ok("\n".join(files) if files else "(empty)")

    tools_ = [create_document, send_file, list_files]

    if images:

        @tool(
            "create_image",
            "Generate a picture from a description and send it to the user as a WhatsApp image "
            "(takes 1-3 minutes; tell the user before calling). Describe subject, setting, style, "
            "lighting and what must NOT appear; put any in-image text in quotes. Optional "
            "reference_file: path of an image the user sent (from the attachment note) or one "
            "already in your folder, to edit or use as guidance. A daily limit applies.",
            {"filename": str, "prompt": str, "caption": str, "reference_file": str},
        )
        async def create_image(args: dict) -> dict:
            name = safe_name(args.get("filename") or "gambar", "png")
            ref = (args.get("reference_file") or "").strip()
            ref_path = None
            if ref:
                ref_path = Path(ref).expanduser()
                if not ref_path.is_absolute():
                    ref_path = root / ref_path
                if not ref_path.is_file() or not any(_inside(ref_path, r) for r in roots):
                    return _err("reference_file must be an image the user sent or one in your folder")
            try:
                out = await image_gen.generate(root / "images", name, args.get("prompt") or "", ref_path,
                                         home=codex_home)
            except Exception as e:  # noqa: BLE001
                return _err(str(e))
            try:
                await send_media(chat_id, str(out), (args.get("caption") or "").strip() or None)
            except Exception as e:  # noqa: BLE001
                return _err(f"image saved as images/{name} but sending failed: {e}")
            left = image_gen.quota_left(root / "images")
            return _ok(f"sent {name} ({out.stat().st_size} bytes); kept as images/{name}; {left} left today")

        tools_.append(create_image)

    return create_sdk_mcp_server(name="docs", version="1.0.0", tools=tools_)
