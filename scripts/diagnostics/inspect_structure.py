"""
只读排查脚本：看一份文档被解析成什么结构、哪些块会被跳过。

用法：python -m scripts.diagnostics.inspect_structure <file-or-run-dir>
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from app.document.spans import build_spans, select_auditable, structure_report
from app.utils.loaders import load_document


def resolve(target: str) -> tuple[str, str]:
    path = Path(target)
    if path.is_dir():
        doc = (path / "original_document.md").read_text(encoding="utf-8")
        return doc, target
    loaded = load_document(path)
    return loaded.text, str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("targets", nargs="+")
    for target in parser.parse_args().targets:
        document, name = resolve(target)
        spans = build_spans(document)
        report = structure_report(spans)

        print("=" * 78)
        print(name)
        print(f"  字符数: {len(document)}   块数: {report['blocks']}")
        print(f"  分区: {json.dumps(report['zones'], ensure_ascii=False)}")
        print(
            f"  标题 {report['headings']} 个，表格 {report['tables']} 个，"
            f"按噪声跳过 {report['skipped_noise']} 个；默认送审 {report['auditable']} 个"
        )

        print("  前 24 个块的归属：")
        for span in spans[:24]:
            print(
                f"    {span.label:5} {span.zone:12} 送审={int(span.auditable)} "
                f"{span.no_reason or '':8} {span.preview[:38]}"
            )

        print("  噪声块示例（被跳过）：")
        skipped = [span for span in spans if not span.auditable]
        for span in skipped[:8]:
            print(f"    {span.label} [{span.no_reason}] {span.preview[:40]}")
        print(f"  噪声原因统计: {dict(Counter(span.no_reason for span in skipped))}")
        print(f"  送审块数: {len(select_auditable(spans))} / {len(spans)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
