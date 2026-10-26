"""
只读排查脚本：分析一次运行的分批、跨批次视野与补丁落地情况。

用法：

    python -m scripts.diagnostics.inspect_run <run_id>
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path


def section(prompt: str, name: str) -> str:
    match = re.search(r"^## " + re.escape(name) + r"\n(.*?)(?=\n## |\Z)", prompt, re.S | re.M)
    return match.group(1) if match else ""


def labels(text: str) -> list[str]:
    return re.findall(r"\[([HPTL]\d+)\]", text)


def inspect(run_id: str) -> None:
    run_dir = Path("data/runs") / run_id
    if not run_dir.exists():
        print(f"{run_id}: 不存在")
        return

    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    print("=" * 78)
    print(run_id)
    print(f"  问题数: {len(state.get('audit_findings') or [])}")
    print(
        f"  轮次: {state.get('turn')}  迭代: {state.get('iteration')}  "
        f"Review: {(state.get('review_result') or {}).get('status')}"
    )
    print(f"  用户要求: {len(state.get('user_rules') or [])} 条")

    print("  各轮历史:")
    for entry in state.get("revision_history") or []:
        print(
            f"     第{entry.get('iteration')}轮 [{entry.get('stage')}] "
            f"edits={entry.get('edits')} failed={entry.get('failed')}"
        )

    applied = state.get("applied_patches") or []
    conflicts = state.get("conflict_patches") or []
    print(
        f"  已应用补丁 {len(applied)} 条，涉及 {len({p.get('span_label') for p in applied})} 个段落；"
        f"冲突 {len(conflicts)} 条"
    )
    if conflicts:
        for patch in conflicts[:6]:
            print(
                f"     冲突 {patch.get('patch_id')} {patch.get('span_label')}: {patch.get('conflict_reason')}"
            )

    calls_path = run_dir / "llm_calls.jsonl"
    if not calls_path.exists():
        return
    records = [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]
    print(f"  模型调用: {dict(Counter((r['agent'], r['action'].split('_')[0]) for r in records))}")

    revision_calls = [r for r in records if r["agent"] == "revision"]
    print(f"  Revision 共 {len(revision_calls)} 批，各批实际拿到的文本范围：")

    chunks: list[set[str]] = []
    for record in revision_calls:
        prompt = record["prompt"]
        body = section(prompt, "待处理文本（带段落 ID）")
        chunk_labels = labels(body)
        chunks.append(set(chunk_labels))
        issues = labels(section(prompt, "需要解决的问题"))
        print(
            f"     {record['action']:10} prompt={len(prompt):6}字符  "
            f"文本段={len(chunk_labels):4}个 ({(chunk_labels[0] if chunk_labels else '-')}"
            f" ~ {(chunk_labels[-1] if chunk_labels else '-')})  "
            f"问题提到段落={sorted(set(issues))[:6]}"
        )

    for i in range(len(chunks)):
        for j in range(i + 1, len(chunks)):
            overlap = chunks[i] & chunks[j]
            if overlap:
                print(f"     ! 批次 {i + 1} 与 {j + 1} 的文本交叠: {sorted(overlap)[:8]}")

    for record in [r for r in records if r["agent"] == "review"]:
        prompt = record["prompt"]
        decisions = section(prompt, "本轮补丁与用户决定")
        print(
            f"  Review({record['action']}): prompt={len(prompt)}字符  "
            f"补丁清单条目={len(re.findall(r'^- patch-', decisions, re.M))}  "
            f"含省略标记={prompt.count('此处省略')}处  "
            f"已应用修改小节={len(section(prompt, '已应用的修改'))}字符  "
            f"修改后文本小节={len(section(prompt, '修改后文本'))}字符"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_ids", nargs="+")
    for run_id in parser.parse_args().run_ids:
        inspect(run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
