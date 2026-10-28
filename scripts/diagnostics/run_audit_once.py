"""
只跑一次Audit（真实模型），用来对比精度改进。

用法：python -m scripts.diagnostics.run_audit_once <file-or-run-dir>
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.agents import audit as audit_agent
from app.document.spans import build_spans, select_auditable, structure_report


def load(target: str) -> tuple[str, str]:
    path = Path(target)
    if path.is_dir():
        return (path / "original_document.md").read_text(encoding="utf-8"), target
    from app.utils.loaders import load_document

    loaded = load_document(path)
    return loaded.text, str(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target")
    target = parser.parse_args().target
    document, name = load(target)
    spans = build_spans(document)
    report = structure_report(spans)

    print("=" * 78)
    print(f"输入：{name}")
    print(
        f"  字符 {len(document)}｜块 {report['blocks']}｜分区 {json.dumps(report['zones'], ensure_ascii=False)}"
    )
    print(f"  送审 {len(select_auditable(spans))} 块｜跳过噪声 {report['skipped_noise']} 块")

    state = {
        "run_id": "audit-precision-check",
        "user_request": "请全面检查并修改这篇论文。",
        "current_document": document,
        "document_version": 1,
    }
    update = audit_agent.run(state, logger=print)
    findings = update.get("audit_findings") or []
    meta = update.get("audit_meta") or {}

    print("\n--- 结果 ---")
    print(f"  问题数：{len(findings)}")
    print(f"  丢弃无法核对：{meta.get('dropped_unverifiable')}")
    print(f"  丢弃低价值(low)：{meta.get('dropped_low_severity')}")
    print(f"  同类合并：{meta.get('merged_systematic')}")
    print(f"  审查范围：{meta.get('scope')}｜送审块：{meta.get('auditable_blocks')}")

    order = {"high": 0, "medium": 1, "low": 2}
    for issue in sorted(findings, key=lambda item: order.get(item.get("severity"), 3)):
        spans_hint = ""
        if issue.get("affected_spans") and len(issue["affected_spans"]) > 1:
            spans_hint = f"｜涉及 {len(issue['affected_spans'])} 处"
        print(
            f"  [{issue.get('id'):02}] {issue.get('severity'):6} {issue.get('category'):6} "
            f"{str(issue.get('span_label')):5} {str(issue.get('problem'))[:70]}{spans_hint}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
