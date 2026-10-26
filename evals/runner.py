"""
评测运行器。

流程：

    Golden Set 中的每条样本
        ↓
    跑一遍完整 Workflow（Audit → Human（自动批准）→ Revision → Review）
        ↓
    LLM-as-Judge 对“修改前 / 修改后”按四个维度打分
        ↓
    汇总成评测结果 JSON（带系统指纹，供回归比较）

用法::

    python -m evals.runner --limit 5 --tag prompt-v2
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from app.config import get_settings
from app.utils.llm import describe_configuration
from app.utils.tracing import get_recorder
from app.workflow.harness import Harness, new_run_id
from evals.judges import DIMENSIONS, judge_pair

REQUEST_TEMPLATE = (
    "请检查这段学术文本在“{dimension}”方面是否存在问题，"
    "并在不改变原意、不改变任何数据与结论的前提下修改它。"
)


def load_golden_set(path: str | Path | None = None) -> list[dict[str, Any]]:
    """读取黄金评测集（JSONL，每行一条）。"""

    golden_path = Path(path) if path else get_settings().paths.golden_set
    if not golden_path.exists():
        raise FileNotFoundError(f"找不到黄金评测集：{golden_path}")

    items: list[dict[str, Any]] = []
    for line in golden_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        items.append(json.loads(line))
    return items


def system_fingerprint() -> dict[str, Any]:
    """记录本次评测用的系统配置，便于判断两次结果是否可比。"""

    settings = get_settings()
    return {
        "model": settings.llm.model,
        "json_mode": settings.llm.json_mode,
        "max_iterations": settings.workflow.max_iterations,
        "audit_chunk_tokens": settings.context.audit_chunk_tokens,
        "temperatures": {
            "orchestrator": settings.agents.orchestrator_temperature,
            "audit": settings.agents.audit_temperature,
            "revision": settings.agents.revision_temperature,
            "review": settings.agents.review_temperature,
        },
    }


def evaluate_item(
    item: dict[str, Any],
    *,
    harness_factory: Callable[..., Harness] | None = None,
    judge: Callable[..., dict[str, Any]] = judge_pair,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """对一条样本跑完整流水线并打分。"""

    text = item.get("text") or ""
    dimension = item.get("dimension") or "语言表达"
    request = REQUEST_TEMPLATE.format(dimension=dimension)

    factory = harness_factory or Harness
    harness = factory(auto_approve=True, logger=lambda message: None)
    result = harness.run_to_completion(request, text, run_id=new_run_id(f"eval-{item.get('id')}"))

    revised = result.revised_document or text
    recorder = get_recorder(harness.run_id)

    judgement = judge(
        text,
        revised,
        focus=dimension,
        expectation=item.get("expectation", ""),
        must_keep=item.get("must_keep") or [],
        recorder=recorder,
        run_id=harness.run_id,
    )

    record = {
        "id": item.get("id"),
        "dimension": dimension,
        "run_id": harness.run_id,
        "run_status": result.status,
        "review_status": result.review_status,
        "iteration": result.state.get("iteration", 0),
        "edits": len(result.state.get("revision_edits") or []),
        "issues": len(result.state.get("audit_findings") or []),
        "changed": revised.strip() != text.strip(),
        "original": text,
        "revised": revised,
        "scores": judgement.get("scores", {}),
        "average": judgement.get("average", 0.0),
        "preserved_meaning": judgement.get("preserved_meaning", False),
        "changed_facts": judgement.get("changed_facts", []),
        "reason": judgement.get("reason", ""),
        "error": judgement.get("error", ""),
        "report_path": result.report_path,
    }
    log(
        f"[Eval] {record['id']}（{dimension}）平均分 {record['average']}"
        f"，Review={record['review_status']}，修改 {record['edits']} 处"
    )
    return record


def evaluate(
    *,
    limit: int | None = None,
    tag: str = "",
    output: str | Path | None = None,
    golden_path: str | Path | None = None,
    harness_factory: Callable[..., Harness] | None = None,
    judge: Callable[..., dict[str, Any]] = judge_pair,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """跑一遍完整评测并保存结果。"""

    items = load_golden_set(golden_path)
    if limit:
        items = items[:limit]

    log(f"[Eval] 共 {len(items)} 条样本，模型：{get_settings().llm.model}")

    records: list[dict[str, Any]] = []
    for item in items:
        try:
            records.append(
                evaluate_item(item, harness_factory=harness_factory, judge=judge, log=log)
            )
        except Exception as exc:  # noqa: BLE001 - 单条样本失败不能让整次评测中断
            log(f"[Eval] {item.get('id')} 执行失败：{type(exc).__name__}: {exc}")
            records.append(
                {
                    "id": item.get("id"),
                    "dimension": item.get("dimension"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "scores": {dimension: 0 for dimension in DIMENSIONS},
                    "average": 0.0,
                    "preserved_meaning": False,
                    "changed_facts": [],
                    "review_status": "",
                    "run_status": "failed",
                }
            )

    result = {
        "tag": tag,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "fingerprint": system_fingerprint(),
        "llm": describe_configuration(),
        "aggregate": aggregate(records),
        "items": records,
    }

    path = Path(output) if output else default_output_path(tag)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    result["output_path"] = str(path)

    log(f"[Eval] 结果已保存：{path}")
    print(format_summary(result))
    return result


def default_output_path(tag: str) -> Path:
    settings = get_settings()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = f"-{tag}" if tag else ""
    return settings.paths.runs_dir / "evals" / f"eval{suffix}-{stamp}.json"


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    """汇总各维度平均分。"""

    valid = [record for record in records if not record.get("error")]
    per_dimension: dict[str, float] = {}
    for dimension in DIMENSIONS:
        values = [record["scores"].get(dimension, 0) for record in valid if record.get("scores")]
        per_dimension[dimension] = round(sum(values) / len(values), 3) if values else 0.0

    averages = [record.get("average", 0.0) for record in valid]
    passes = [record for record in valid if record.get("review_status") == "pass"]
    changed = [record for record in valid if record.get("changed")]

    return {
        "items": len(records),
        "scored": len(valid),
        "errors": len(records) - len(valid),
        "dimensions": per_dimension,
        "overall": round(sum(averages) / len(averages), 3) if averages else 0.0,
        "review_pass_rate": round(len(passes) / len(valid), 3) if valid else 0.0,
        "changed_rate": round(len(changed) / len(valid), 3) if valid else 0.0,
        "meaning_preserved_rate": round(
            sum(1 for record in valid if record.get("preserved_meaning")) / len(valid), 3
        )
        if valid
        else 0.0,
    }


def format_summary(result: dict[str, Any]) -> str:
    aggregate_data = result["aggregate"]
    lines = ["", "========== 评测汇总 =========="]
    lines.append(f"样本数：{aggregate_data['items']}（有效 {aggregate_data['scored']}）")
    for dimension, score in aggregate_data["dimensions"].items():
        lines.append(f"  {dimension}：{score}")
    lines.append(f"  综合平均分：{aggregate_data['overall']}")
    lines.append(f"  Review 通过率：{aggregate_data['review_pass_rate']}")
    lines.append(f"  原意保持率：{aggregate_data['meaning_preserved_rate']}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="论文修改系统评测（Golden Set + LLM-as-Judge）")
    parser.add_argument("--limit", type=int, default=None, help="只评测前 N 条")
    parser.add_argument("--tag", type=str, default="", help="本次评测的标签（例如 prompt-v2）")
    parser.add_argument("--output", type=str, default="", help="结果文件路径")
    parser.add_argument("--golden", type=str, default="", help="黄金集路径")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    evaluate(
        limit=args.limit,
        tag=args.tag,
        output=args.output or None,
        golden_path=args.golden or None,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
