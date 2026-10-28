"""文档模型层：可寻址的段落切分（span）、补丁（patch）与Word回写。"""

from app.document.docx_model import DocxBlock, DocxSource, Piece
from app.document.docx_redline import (
    RedlineOutcome,
    applied_rounds,
    export_redline,
)
from app.document.patches import (
    Patch,
    apply_patches,
    next_patch_id,
    patch_from_model,
    validate_patch,
)
from app.document.spans import (
    Span,
    build_spans,
    index_summary,
    render_annotated,
    render_outline,
    resolve_span,
    sentence_id,
    split_sentences,
)

__all__ = [
    "DocxBlock",
    "DocxSource",
    "Patch",
    "Piece",
    "RedlineOutcome",
    "Span",
    "applied_rounds",
    "apply_patches",
    "build_spans",
    "export_redline",
    "index_summary",
    "next_patch_id",
    "patch_from_model",
    "render_annotated",
    "render_outline",
    "resolve_span",
    "sentence_id",
    "split_sentences",
    "validate_patch",
]
