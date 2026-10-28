"""Immutable Word → PDF → page images, with conservative text anchoring.

No browser reflow or guessed page numbers. Ambiguous/missing anchors are blocked.
"""

import json
import math
import os
import re
import shutil
import subprocess
import tempfile
from bisect import bisect_left
from hashlib import sha256
from pathlib import Path

from app.document.docx_model import DocxSource
from app.document.docx_redline import Unlocatable, _locate
from app.document.final_review import compose_changes, deduplicate, unfinished_items
from app.document.spans import build_spans


def compact(text):
    return "".join(c for c in text if not c.isspace())


def _contextual_page_index(original, spans, rendered, start, end, before):
    """Disambiguate TOC/body duplicates using exact adjacent visible text.

    Strip only parser-added heading/list prefixes, retaining original offsets.
    Never pick the first/last occurrence or guess a page from a section number.
    """
    decoration = set()
    for span in spans:
        prefix = (
            re.match(r"^#{1,6}\s+", span.text)
            if span.kind == "heading"
            else (re.match(r"^-\s+", span.text) if span.kind == "list" else None)
        )
        if prefix:
            decoration.update(range(span.start, span.start + prefix.end()))
    offsets = [i for i, c in enumerate(original) if not c.isspace() and i not in decoration]
    visible = "".join(original[i] for i in offsets)
    a, b = bisect_left(offsets, start), bisect_left(offsets, end)
    if visible[a:b] != before:
        return None
    candidates = set()
    for radius in (8, 16, 32, 64, 128, 256, 512):
        for left, right in (
            (max(0, a - radius), b),
            (a, min(len(visible), b + radius)),
            (max(0, a - radius), min(len(visible), b + radius)),
        ):
            context = visible[left:right]
            if visible.count(context) == rendered.count(context) == 1:
                index = rendered.index(context) + a - left
                if rendered[index : index + len(before)] == before:
                    candidates.add(index)
    return next(iter(candidates)) if len(candidates) == 1 else None


def manual_patch_suggestion(patch):
    if patch.get("action", "replace_text") != "replace_text":
        from app.document.structured import describe_operation

        return (
            f"请在「{patch.get('before') or patch.get('quote') or ''}」处人工操作："
            + describe_operation(patch["action"], patch.get("params") or {})
        )
    return f"请人工将「{patch.get('before') or patch.get('quote') or ''}」改为「{patch.get('replace') or '（删除）'}」"


def render_pages(source_path, directory):
    import pdfplumber

    source_path, directory = Path(source_path), Path(directory)
    fingerprint = sha256(source_path.read_bytes() + b"preview-v1").hexdigest()
    cache = directory / fingerprint
    metadata = cache / "pages.json"
    if metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        if all((cache / f"page-{p['number']}.png").is_file() for p in data["pages"]):
            return data, cache
    executable = (
        os.getenv("PRA_LIBREOFFICE") or shutil.which("soffice") or shutil.which("libreoffice")
    )
    if not executable:
        raise ValueError("分页预览不可用：未安装LibreOffice，未定位的修改不会写入")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pra-preview-") as folder:
        root = Path(folder)
        (root / "original.docx").write_bytes(source_path.read_bytes())
        profile = root / "profile" / "user"
        profile.mkdir(parents=True)
        (profile / "registrymodifications.xcu").write_text(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
            '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
            "</item></oor:items>",
            encoding="utf-8",
        )
        try:
            proc = subprocess.run(
                [
                    executable,
                    f"-env:UserInstallation={(root / 'profile').as_uri()}",
                    "--headless",
                    "--nologo",
                    "--norestore",
                    "--convert-to",
                    "pdf:writer_pdf_Export",
                    "--outdir",
                    str(root),
                    str(root / "original.docx"),
                ],
                timeout=120,
                capture_output=True,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("原文分页转换失败或超过120秒，未定位的修改不会写入") from exc
        pdf = root / "original.pdf"
        if proc.returncode or not pdf.is_file() or pdf.stat().st_size > 80 * 1024 * 1024:
            raise ValueError("原文分页转换失败或文件超限，未定位的修改不会写入")
        pages, chars = [], []
        pixels = total_bytes = 0
        with pdfplumber.open(pdf) as document:
            if len(document.pages) > 150:
                raise ValueError("论文超过150页预览上限，未定位的修改不会写入")
            for number, page in enumerate(document.pages, 1):
                width, height = float(page.width), float(page.height)
                pixels += width * height * (96 / 72) ** 2
                if (
                    not all(math.isfinite(v) and 0 < v <= 3000 for v in (width, height))
                    or pixels > 150_000_000
                ):
                    raise ValueError("预览页面尺寸或总像素超限")
                target = cache / f"page-{number}.png"
                page.to_image(resolution=96).save(target, format="PNG")
                total_bytes += target.stat().st_size
                if total_bytes > 160 * 1024 * 1024:
                    raise ValueError("预览图像总大小超限")
                pages.append({"number": number, "width": width, "height": height})
                for char in page.chars:
                    for letter in char["text"]:
                        if not letter.isspace():
                            chars.append(
                                [
                                    letter,
                                    number,
                                    float(char["x0"]) / width,
                                    float(char["top"]) / height,
                                    float(char["x1"]) / width,
                                    float(char["bottom"]) / height,
                                ]
                            )
                if len(chars) > 1_000_000:
                    raise ValueError("预览字符数量超限")
        data = {"fingerprint": fingerprint, "pages": pages, "chars": chars}
        temp = cache / "pages.tmp"
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temp.replace(metadata)
        return data, cache


def locate_on_pages(patch, original, page_data):
    """Require a unique exact contextual match in BOTH original and rendered text."""
    chars = page_data["chars"]
    rendered = "".join(char[0] for char in chars)
    normalized_original = compact(original)
    a, b = patch["original_start"], patch["original_end"]
    spans = build_spans(original)
    span = next(s for s in spans if s.start <= a < b <= s.end)
    before = compact(original[a:b])
    if not before:
        raise ValueError("仅空白修改无法可靠标红定位")
    index = None
    if patch.get("action") == "table_edit":
        # Markdown pipes/separator rows do not exist on the PDF page. Locate
        # the complete visible table, never a repeated cell in isolation.
        def table_text(value):
            import re

            return compact(
                "".join(
                    line.replace("|", "")
                    for line in value.splitlines()
                    if not re.fullmatch(r"[\s|:\-]+", line)
                )
            )

        before = table_text(span.text)
        duplicates = sum(table_text(s.text) == before for s in spans if s.kind == "table")
        if not before or duplicates != 1 or rendered.count(before) != 1:
            raise ValueError("表格无法在原始分页中唯一匹配（可能跨页或重复表头），需人工核对")
        index = rendered.index(before)
    line_start = max(span.start, original.rfind("\n", span.start, a) + 1)
    newline = original.find("\n", b, span.end)
    line_end = newline if newline >= 0 else span.end
    for radius in (0, 8, 16, 32, 64, 128):
        if index is not None:
            break
        left, right = max(line_start, a - radius), min(line_end, b + radius)
        context = compact(original[left:right])
        if context and normalized_original.count(context) == rendered.count(context) == 1:
            index = rendered.index(context) + len(compact(original[left:a]))
            break
    if index is None and patch.get("action") != "table_edit":
        from app.document.requests import heading_level

        if heading_level(span) is not None:
            index = _contextual_page_index(original, spans, rendered, a, b, before)
    if index is None or rendered[index : index + len(before)] != before:
        raise ValueError("原文位置有歧义或渲染文字不一致，未自动写入")
    boxes = []
    for _, page, x0, y0, x1, y1 in chars[index : index + len(before)]:
        if (
            not all(math.isfinite(v) and 0 <= v <= 1 for v in (x0, y0, x1, y1))
            or x1 <= x0
            or y1 <= y0
        ):
            raise ValueError("渲染字符坐标无效，未自动写入")
        if (
            boxes
            and boxes[-1]["page"] == page
            and abs(boxes[-1]["y"] - y0) < 0.004
            and -0.003 <= x0 - (boxes[-1]["x"] + boxes[-1]["width"]) < 0.012
        ):
            box = boxes[-1]
            box["width"] = x1 - box["x"]
            box["height"] = max(box["height"], y1 - box["y"])
        else:
            boxes.append({"page": page, "x": x0, "y": y0, "width": x1 - x0, "height": y1 - y0})
    return boxes


def prepare_visual_review(state, run_dir):
    original = state.get("original_document") or ""
    from app.document.patches import Patch
    from app.document.review_items import decision_conflict, manual_item, manual_reason, public_item

    manual = list(state.get("manual_items") or [])
    reason = manual_reason(state.get("user_request"))
    if reason and not any(
        m.get("source") == "user_request" or m.get("blocked_reason") == reason for m in manual
    ):
        manual.append(
            manual_item(
                {
                    "problem": state["user_request"],
                    "suggestion": state["user_request"],
                    "source": "user_request",
                },
                reason,
            )
        )
    # Keep pre-confirmation failures in diagnostics, not in promised edits.
    diagnostics = []
    try:
        patches = compose_changes(state)
    except (ValueError, KeyError, TypeError) as exc:
        patches = []
        diagnostics.append({"title": "最终修改无法安全合并", "reason": str(exc), "kind": "blocked"})
        for row in state.get("revision_history") or []:
            for item in row.get("applied") or []:
                manual.append(
                    manual_item(
                        item,
                        str(exc),
                        f"请人工核对将「{item.get('before', '')}」改为「{item.get('replace', '')}」",
                    )
                )
    preview = {"pages": [], "error": "", "fingerprint": ""}
    try:
        source = DocxSource(Path(state["source_docx"]).read_bytes())
        if source.text != original:
            raise ValueError("原始Word与审查文本版本不一致，已停止自动写入")
        data, _ = render_pages(state["source_docx"], Path(run_dir) / "preview")
        preview.update({"pages": data["pages"], "fingerprint": data["fingerprint"]})
    except Exception as exc:
        preview["error"] = "无法生成可靠的分页预览，请检查转换器和原文件。"
        data = None
        diagnostics.append({"title": "分页预览不可用", "reason": str(exc), "kind": "blocked"})
    ready = []
    advice = []  # Legacy field retained for checkpoint compatibility; never exported.
    for issue in (state.get("audit_findings") or []) + (state.get("deferred_advice") or []):
        if issue.get("kind") != "advice":
            continue
        if issue.get("ignored"):
            continue
        reason = issue.get("blocked_reason") or manual_reason(issue.get("suggestion"))
        if issue.get("manual_required") or reason:
            manual.append(manual_item(issue, reason or "此项修改需要作者人工处理"))
            continue
        # A raw audit opinion is not a promised modification. Revision attempts
        # it first; any actual failure is captured in manual_items above.
    spans = build_spans(original)
    for patch in patches:
        try:
            conflict = decision_conflict(
                Patch.from_json(patch), state.get("selected_decisions"), spans
            )
            if conflict:
                raise ValueError(conflict)
            if data is None:
                raise ValueError("分页预览不可用，无法让用户核对修改位置，保留原文")
            from app.document.structured import is_structured, preflight

            if is_structured(patch):
                preflight(Path(state["source_docx"]).read_bytes(), patch)
                if patch.get("action") == "table_edit" and (patch.get("params") or {}).get(
                    "operation"
                ) in {"add_column", "delete_column", "merge_cells", "split_cell"}:
                    patch["review_note"] = (
                        "此项会改变表格网格，并添加操作批注；请在网页取消勾选以保留原文。暂不支持在Word中按文字修订方式单独拒绝该网格操作。"
                    )
            else:
                _locate(patch, source=source, spans=spans, text=original)
            boxes = locate_on_pages(patch, original, data)
            # Concrete semantic concerns are visible BEFORE the user accepts
            # this edit, rather than misreported afterward as write failures.
            concrete = [
                i
                for i in (state.get("review_result") or {}).get("remaining_issues") or []
                if str(i.get("id")) in {str(n) for n in patch.get("issue_ids") or []}
                and i.get("evidence")
                and i["evidence"] in (state.get("current_document") or "")
            ]
            if concrete:
                patch["review_note"] = "\n".join(
                    filter(
                        None,
                        [
                            patch.get("review_note"),
                            *["请在确认前核对：" + str(i.get("problem") or "") for i in concrete],
                        ],
                    )
                )
            ready.append(
                public_item(
                    {
                        **patch,
                        "number": len(ready) + 1,
                        "rects": boxes,
                        "original_checksum": sha256(
                            Path(state["source_docx"]).read_bytes()
                        ).hexdigest(),
                    },
                    original,
                )
            )
        except (Unlocatable, ValueError) as exc:
            diagnostics.append(
                {
                    "title": patch["before"],
                    "reason": str(exc),
                    "position": patch["span_label"],
                    "kind": "blocked",
                }
            )
            choice = next(
                (
                    d
                    for d in reversed(state.get("selected_decisions") or [])
                    if d.get("evidence")
                    and (patch["before"] in d["evidence"] or d["evidence"] in patch["before"])
                ),
                None,
            )
            suggestion = choice["selected_option"] if choice else manual_patch_suggestion(patch)
            manual.append(
                manual_item(
                    {**patch, "original_anchor_start": patch["original_start"]},
                    str(exc),
                    suggestion,
                )
            )
    for row in state.get("revision_history") or []:
        for item in row.get("failed_edits") or []:
            quote = item.get("before") or item.get("quote") or ""
            if not quote and not item.get("replace"):
                continue
            if (
                item.get("action", "replace_text") == "replace_text"
                and not item.get("replace")
                and not item.get("delete")
            ):
                continue  # Never invent a deletion suggestion from invalid output.
            ids = {
                str(i)
                for i in [item.get("issue_id"), *(item.get("issue_ids") or [])]
                if i is not None
            }
            if ids and any(ids.intersection(str(i) for i in p.get("issue_ids", [])) for p in ready):
                continue
            if item.get("conflict_code") in {"no_change", "code_protected"}:
                continue
            if decision_conflict(
                Patch.from_json(item),
                state.get("selected_decisions"),
                build_spans(state.get("current_document") or original),
            ):
                # The actual chosen manual solution is already listed; never
                # re-introduce the rejected alternative as a manual suggestion.
                continue
            if item.get("patch_id") not in {
                p for r in ready for p in r.get("source_patch_ids", [])
            }:
                if quote and quote not in original:
                    issue = next(
                        (i for i in state.get("audit_findings") or [] if str(i.get("id")) in ids),
                        None,
                    )
                    if not issue:
                        continue  # An invented quote is not an author task.
                    manual.append(
                        manual_item(
                            issue,
                            "生成方案未能与原文准确对齐，未执行该修改",
                            issue.get("suggestion"),
                        )
                    )
                else:
                    manual.append(
                        manual_item(
                            item,
                            item.get("conflict_reason") or item.get("reason") or "修改未能安全定位",
                            manual_patch_suggestion(item),
                        )
                    )
    frozen = []
    for item in {m["manual_id"]: m for m in manual}.values():
        if (
            item.get("source") != "explicit_citation"
            and item.get("id") is not None
            and any(item["id"] in p.get("issue_ids", []) for p in ready)
        ):
            continue
        quote = (
            item.get("audit_anchor_text")
            or item.get("evidence")
            or item.get("before")
            or item.get("quote")
            or ""
        )
        start = item.get("original_anchor_start", -1)
        if start < 0 and quote and original.count(quote) == 1:
            start = original.index(quote)
        item = {**item, "original_anchor_start": start}
        if start >= 0:
            span = next((s for s in spans if s.start <= start < s.end), None)
            if span:
                item["span_label"] = span.label
        frozen.append(public_item(item, original))
    return {
        "final_patches": ready,
        "preview_manifest": preview,
        "final_manual_items": frozen,
        "unfinished_items": unfinished_items({**state, "final_patches": ready}),
        "review_diagnostics": deduplicate(diagnostics),
        "deferred_advice": advice,
    }
