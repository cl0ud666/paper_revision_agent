"""
只读排查脚本：看清"论文被读成了什么"，以及Audit报的问题是真问题还是噪声。

用法：python -m scripts.diagnostics.inspect_audit <run_id>
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id")
    run_id = parser.parse_args().run_id
    run_dir = Path("data/runs") / run_id
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    document = (run_dir / "original_document.md").read_text(encoding="utf-8")
    spans = state.get("spans") or []
    findings = state.get("audit_findings") or []

    print("=" * 78)
    print(f"文档：{len(document)} 字符，AI看到的块：{len(spans)} 个")

    print("\n--- 块的类型分布 ---")
    print("   ", dict(Counter(span.get("kind") for span in spans)))

    tiny = [span for span in spans if (span.get("chars") or 0) < 30]
    print(f"\n--- 过短块（<30字符）：{len(tiny)} 个 —— 这类基本是封面字段/页码/目录行 ---")
    for span in tiny[:25]:
        print(f"    {span.get('label')} [{span.get('kind')}] {span.get('preview')}")

    toc = [span for span in spans if re.search(r"\.{4,}\s*\d+\s*$", span.get("preview") or "")]
    numbers = [
        span for span in spans if re.fullmatch(r"\s*[-–—]?\s*\d{1,3}\s*", span.get("preview") or "")
    ]
    print(f"\n--- 目录行（带点线+页码）：{len(toc)} 个；纯页码行：{len(numbers)} 个 ---")
    for span in toc[:6]:
        print(f"    {span.get('label')} {span.get('preview')[:60]}")

    print("\n--- 前30个块的段落归属（看结构是否还清楚）---")
    for span in spans[:30]:
        print(
            f"    {span.get('label'):5} [{span.get('kind'):9}] "
            f"章节={str(span.get('section'))[:24]:26} {(span.get('preview') or '')[:44]}"
        )

    print("\n--- Audit报的 {n} 条问题 ---".format(n=len(findings)))
    print("  严重程度:", dict(Counter(issue.get("severity") for issue in findings)))
    print("  分类:", dict(Counter(issue.get("category") for issue in findings)))

    by_span = {span.get("label"): span for span in spans}
    checkable = 0
    evidence_missing = []
    front_matter = []
    for issue in findings:
        label = issue.get("span_label")
        span = by_span.get(label) or {}
        text = span.get("preview") or ""
        evidence = (issue.get("evidence") or "").strip()
        if evidence and evidence in text:
            checkable += 1
        elif evidence:
            evidence_missing.append(issue)
        section = str(issue.get("location") or span.get("section") or "")
        if re.search(r"封面|目录|承诺|致谢|题目|学院|学号|姓名|指导教师|完成日期", section):
            front_matter.append(issue)

    print(f"\n能在对应块里找到evidence的：{checkable}/{len(findings)}")
    print(f"  evidence与块内容对不上的：{len(evidence_missing)} 条（这些是幻觉/错位的重点嫌疑）")
    print(f"  目标落在封面/目录/承诺页等非正文部分：{len(front_matter)} 条")

    print("\n--- 全部问题（按严重程度）---")
    order = {"high": 0, "medium": 1, "low": 2}
    for issue in sorted(findings, key=lambda item: order.get(item.get("severity"), 3)):
        span = by_span.get(issue.get("span_label")) or {}
        mark = "OK " if (issue.get("evidence") or "") in (span.get("preview") or "") else "!! "
        print(
            f"  {mark}[{issue.get('id'):02}] {issue.get('severity'):6} "
            f"{str(issue.get('category'))[:6]:6} {str(issue.get('span_label')):5} "
            f"块内容={str(span.get('preview'))[:28]:30} 问题={str(issue.get('problem'))[:52]}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
