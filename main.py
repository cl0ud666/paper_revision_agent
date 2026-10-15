"""
学术论文智能修改系统 —— 命令行入口。

常用命令：

    # 1) 检查 DeepSeek API 是否连通
    python main.py --ping

    # 2) 查看 Workflow 结构（Mermaid）
    python main.py --show-graph

    # 3) 全面检查并修改一篇论文（中途会停下来让你确认修改范围）
    python main.py --file data/samples/sample_paper.md --request "全面检查并修改这篇论文"

    # 4) 无人值守模式（自动批准全部问题）
    python main.py --file data/samples/sample_paper.md --auto-approve ^
        --constraint "不要修改实验结果" --constraint "保持作者原有表达风格"

    # 5) 只改某一段（Direct Revision，跳过全文审查）
    python main.py --text "摘要写得太啰嗦了" --request "只修改摘要，不要审查全文"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.utils.llm import describe_configuration, ping
from app.utils.loaders import DocumentLoadError, load_document
from app.workflow.graph import build_graph, mermaid
from app.workflow.harness import Harness
from app.workflow.interaction import dispatch_request


SEVERITY_LABEL = {"high": "高", "medium": "中", "low": "低"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="学术论文智能修改 Multi-Agent 系统（LangGraph + DeepSeek）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--file",
        "-f",
        type=str,
        default="",
        help="论文文件路径，支持 .docx / .pdf / .md / .txt（含空格请用引号包起来）",
    )
    parser.add_argument("--text", "-t", type=str, default="", help="直接传入论文文本")
    parser.add_argument(
        "--request",
        "-r",
        type=str,
        default="请全面检查并修改这篇论文。",
        help="用户任务描述",
    )
    parser.add_argument(
        "--constraint",
        "-c",
        action="append",
        default=[],
        help="修改约束，可重复传入（例如：不要修改实验结果）",
    )
    parser.add_argument(
        "--rule",
        action="append",
        default=[],
        help="本轮及后续所有轮次都有效的要求，可重复传入"
        "（例如：全文统一把“土豆”写成“马铃薯”）",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="自动批准全部问题（无人值守）",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=None, help="最大修改轮次（默认取配置）"
    )
    parser.add_argument("--run-id", type=str, default="", help="自定义 run_id")
    parser.add_argument("--show-graph", action="store_true", help="打印并导出 Workflow 结构")
    parser.add_argument("--setup-checkpoint", action="store_true", help="初始化持久化 Checkpoint 数据库（部署时执行一次）")
    '''
    show_graph() 什么时候被调用？
靠 main() 里手写的一段判断：

python
def main():
    args = parse_args()          # 解析出 args.show_graph = True/False

    if args.show_graph:          # ← 只有这里才会触发
        show_graph()             # ← 真正调用
        return
'''
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="只解析文件并显示结构（分区/块/噪声），不调用模型、不花钱",
    )
    parser.add_argument("--ping", action="store_true", help="检查 LLM 配置与连通性")
    parser.add_argument("--quiet", "-q", action="store_true", help="减少过程日志")
    return parser.parse_args(argv)
#命令行输入格式解析，比如-f是什么 -c是什么 自定义的

def read_input(args: argparse.Namespace) -> tuple[str, Any]:
    """读取论文内容，并尽量带上"文件加载结果"（含能否写回 Word）。"""

    if args.text:
        return args.text, None

    if args.file:
        path = Path(args.file)
        if not path.is_absolute():
            path = get_settings().paths.project_root / path
        try:
            loaded = load_document(path)
        except DocumentLoadError as exc:
            raise SystemExit(f"读取论文失败：{exc}") from exc
        print(f"[CLI] 已读取 {path.name}：{loaded.describe()}")
        return loaded.text, loaded

    default_sample = get_settings().paths.samples_dir / "sample_paper.md"
    if default_sample.exists():
        print(f"[CLI] 未指定论文，使用示例文件：{default_sample}")
        return default_sample.read_text(encoding="utf-8"), None

    raise SystemExit("请通过 --file 或 --text 提供论文内容。")


def read_document(args: argparse.Namespace) -> str:
    """读取论文内容（只要文字）。"""

    return read_input(args)[0]


def source_docx_path(args: argparse.Namespace) -> str:
    """
    原始 .docx 的路径（用于把改动写回 Word）。

    只有"从文件读的 Word"才有这条路径：改完会在 data/runs/<run_id>/
    另存一份带修订痕迹的 revised_document.docx，原文件不动。
    """

    if not args.file or args.text:
        return ""
    path = Path(args.file)
    if not path.is_absolute():
        path = get_settings().paths.project_root / path
    if path.suffix.lower() == ".docx" and path.exists():
        return str(path)
    return ""


def show_graph() -> None:
    """打印 Workflow 结构，并保存为 workflow_graph.mmd。"""

    graph = build_graph()
    code = mermaid(graph)
    print("\n========== GRAPH STRUCTURE ==========")
    print(code)

    output = get_settings().paths.project_root / "workflow_graph.mmd"
    output.write_text(code, encoding="utf-8")
    print(f"\n已保存到：{output}")


def show_structure(document: str, name: str = "", *, loaded: Any = None) -> None:
    """
    结构自检：把论文解析成什么，先给用户看清楚（不调用模型、不花钱）。

    专门用来回答"我排版好的论文被读成了一团文字流"这类问题：
    分区、块数、被跳过的噪声、前若干个块，一眼就能看出解析对不对。
    """

    from app.document.spans import (
        ZONE_NAMES,
        build_spans,
        select_auditable,
        structure_report,
    )

    spans = build_spans(document)
    report = structure_report(spans)
    auditable = select_auditable(spans)

    print(f"\n========== 结构自检：{name} ==========")
    print(f"字符数：{len(document)}    块数：{report['blocks']}")
    print(
        "分区："
        + "，".join(
            f"{ZONE_NAMES.get(zone, zone)} {count}"
            for zone, count in sorted(report["zones"].items(), key=lambda item: -item[1])
        )
    )
    print(
        f"标题 {report['headings']} 个，表格 {report['tables']} 个，"
        f"按噪声跳过 {report['skipped_noise']} 个"
    )
    print(f"默认送审（摘要+正文）：{len(auditable)} 块（可用 PRA_AUDIT_SCOPE=body+refs / all 调整）")

    stats = getattr(loaded, "docx_stats", None) or {}
    if getattr(loaded, "can_write_back", False):
        print(
            "写回 Word：可用"
            f"（可编辑块 {stats.get('writable_blocks', '-')}/{stats.get('blocks', '-')}，"
            f"含公式的块 {stats.get('with_formula', 0)}，含图片的块 {stats.get('with_object', 0)}）"
            "；改动会以 Word 修订痕迹 + 批注写回副本，原文件不动"
        )
    elif loaded is not None and loaded.format == "docx":
        print("写回 Word：不可用（没装 lxml，只能输出 Markdown）")

    print("\n--- 前 16 个块 ---")
    for span in spans[:16]:
        flag = f"（跳过：{span.no_reason}）" if not span.auditable else ""
        print(
            f"  {span.label:5} [{ZONE_NAMES.get(span.zone, span.zone)}] {flag}"
            f"{span.preview[:44]}"
        )

    warnings: list[str] = []
    if report["headings"] == 0:
        warnings.append("没有识别到任何标题：结构可能已丢失，建议改用 .docx 上传。")
    if report["blocks"] and report["skipped_noise"] / report["blocks"] > 0.4:
        warnings.append(
            f"被跳过的噪声块占 {report['skipped_noise'] / report['blocks']:.0%}（目录/页码等）；"
            "如果这是 PDF，建议改用 .docx 以保留结构。"
        )
    body_spans = [span for span in spans if span.zone in {"abstract", "body"} and span.auditable]
    if body_spans:
        average = sum(len(span.text) for span in body_spans) / len(body_spans)
        if average < 50:
            warnings.append(f"正文块平均只有 {average:.0f} 字符，可能是被 PDF 拆成了碎片。")
    for warning in warnings:
        print(f"\n⚠ {warning}")
    if not warnings:
        print("\n结构看起来正常：标题层级与段落边界都识别到了。")


def ask_human(request: dict) -> dict:
    """
    命令行下的人机协作：展示问题清单，收集用户选择与约束。

    非交互环境（stdin 不可用）时自动批准全部问题。
    """

    issues = request.get("issues") or []
    print("\n========== 需要你确认的修改范围 ==========")
    for issue in issues:
        severity = SEVERITY_LABEL.get(issue.get("severity", ""), issue.get("severity", ""))
        print(
            f"[{issue.get('id')}] ({severity} / {issue.get('category')}) "
            f"{issue.get('problem')}"
        )
        if issue.get("location"):
            print(f"      位置：{issue['location']}")
        if issue.get("suggestion"):
            print(f"      建议：{issue['suggestion']}")

    try:
        raw_ids = input(
            "\n请输入要修改的问题编号（逗号分隔；直接回车=全部；none=都不修改）："
        ).strip()
        raw_constraints = input(
            "请输入修改约束（多条用分号分隔，可留空）："
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[CLI] 无法读取输入，自动批准全部问题。")
        return {
            "decision": "approve",
            "approved_issue_ids": "all",
            "constraints": [],
            "feedback": "（非交互环境自动批准）",
        }

    if raw_ids.lower() in {"none", "不修改", "无"}:
        approved: object = []
    elif not raw_ids:
        approved = "all"
    else:
        approved = raw_ids

    constraints = [
        piece.strip()
        for piece in raw_constraints.replace("；", ";").split(";")
        if piece.strip()
    ]

    return {
        "decision": "approve" if approved != [] else "reject",
        "approved_issue_ids": approved,
        "constraints": constraints,
        "feedback": "",
    }


def ask_patches(request: dict) -> dict:
    """
    补丁审阅（默认也是唯一的人工环节）：
    一次性列出本轮全部改动，每条形如

        [patch-001] P03（第 2 句）｜语言表达（中）
          修改前：……
          修改后：……
          理由：……            <- Agent 写好的，用户不用写

    用户只需要说“要哪些”：回车=全部接受，none=全部拒绝，或输入编号。
    """

    patches = request.get("patches") or []
    decidable = [patch for patch in patches if patch.get("status") == "proposed"]

    print("\n========== 本轮改动清单 ==========")
    print(request.get("summary", ""))
    for patch in patches:
        position = patch.get("position") or patch.get("span_label") or patch.get("span_id")
        label = patch.get("status_label") or patch.get("status")
        print(
            f"\n[{patch.get('patch_id')}] {position}"
            f"｜{patch.get('category', '')}（{SEVERITY_LABEL.get(patch.get('severity', ''), patch.get('severity', ''))}）"
            f"｜{label}"
        )
        if patch.get("before"):
            print(f"    修改前：{patch['before']}")
        if patch.get("replace"):
            print(f"    修改后：{patch['replace']}")
        print(f"    理由：{patch.get('rationale') or '（未提供）'}")
        if patch.get("conflict_reason"):
            print(f"    无法定位：{patch['conflict_reason']}")
        for warning in patch.get("warnings") or []:
            print(f"    提示：{warning}")

    if not decidable:
        print("\n本轮没有可以接受的改动。")
        return {"reject_all": True}

    print("\n可接受的编号：" + "、".join(f"{i + 1}" for i in range(len(decidable))))
    try:
        raw = input("要接受哪些？（回车=全部接受；none=全部拒绝；也可输入 1,3）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[CLI] 无法读取输入，默认全部接受。")
        return {"accept_all": True}

    if raw.lower() in {"none", "不", "都不要"}:
        return {"reject_all": True}
    if not raw:
        return {"accept_all": True}

    wanted: list[str] = []
    for piece in raw.replace("，", ",").split(","):
        piece = piece.strip()
        if piece.isdigit():
            index = int(piece) - 1
            if 0 <= index < len(decidable):
                wanted.append(str(decidable[index].get("patch_id")))
        elif piece:
            wanted.append(piece)  # 也允许直接写 patch-003
    return {"accepted_ids": wanted}


def ask_final_confirm(request: dict) -> dict:
    """
    写入 Word 之前的最后一次确认：把**跨轮次的全部改动**一次性列出来。

    前面每轮都在内存里改文本（不然下一轮 Audit 看到的还是旧稿），
    但 Word 只在最后写一次——所以确认也只需要一次。
    """

    patches = request.get("patches") or []
    print("\n========== 即将写入 Word 的全部改动 ==========")
    for position, patch in enumerate(patches, start=1):
        where = patch.get("position") or patch.get("span_label") or ""
        if patch.get("count"):
            labels = patch.get("span_labels") or []
            shown = "、".join(labels[:12]) + ("…" if len(labels) > 12 else "")
            where = f"{where}（共 {patch['count']} 处：{shown}）"
        print(f"\n[{position}] {where}")
        print(f"    问题：{patch.get('problem') or patch.get('category') or ''}")
        if patch.get("before"):
            print(f"    原文：{patch['before']}")
        if patch.get("replace"):
            print(f"    改为什么：{patch['replace']}")
        if patch.get("rationale"):
            print(f"    原因：{patch['rationale']}")

    try:
        raw = input(
            "\n要剔除哪几条？（例如 2,5；回车 = 全部写入；none = 都不写入）："
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[CLI] 无法读取输入，默认全部写入。")
        return {"excluded_ids": []}

    if raw.lower() in {"none", "全部不写", "都不要"}:
        return {"exclude_all": True}
    if not raw:
        return {"excluded_ids": []}
    return {"excluded_ids": [piece.strip() for piece in raw.replace("，", ",").split(",") if piece.strip()]}


def ask_issues(request: dict) -> dict:
    """
    Audit 之后的**决策闸门**：只列出"前后矛盾、必须作者拍板"的问题。

    为什么只问这一类：写错的问题（fix）Revision 直接改，可以更好的（advice）
    只写批注，两者都不该打扰用户。真正卡住的只有"两处说法打架、以哪一处为准"——
    实验事实在作者手上，模型看不到，只能问人。

    用户的判断会被记成规则，随 Revision 的上下文一起送达。
    直接回车 = 这些先不改。
    """

    issues = request.get("issues") or []
    print("\n========== 需要你拍板的前后矛盾 ==========")
    for position, issue in enumerate(issues, start=1):
        severity = SEVERITY_LABEL.get(issue.get("severity", ""), issue.get("severity", ""))
        print(
            f"\n[{position}] {issue.get('span_label') or issue.get('location') or ''}"
            f"（{severity}｜{issue.get('category')}）{issue.get('problem')}"
        )
        options = issue.get("options") or []
        if options:
            for offset, option in enumerate(options):
                print(f"      {chr(ord('a') + offset)}) {option}")
        elif issue.get("suggestion"):
            print(f"      模型建议：{issue['suggestion']}")

    remembered = request.get("remembered") or []
    if remembered:
        print("\n你已经确认过不改的（本轮不再问）：")
        for rule in remembered:
            print(f"  [{rule.get('id')}] {rule.get('text')}")

    try:
        raw = input(
            "\n以哪一处为准？按「序号+字母」选，例如 1a 3b；"
            "也可以直接写你自己的判断\n"
            "（回车 = 这些先不改）："
        ).strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[CLI] 无法读取输入，这些矛盾先不改。")
        return {"ignore_ids": [issue["id"] for issue in issues], "rules": [], "turn": request.get("turn")}

    chosen = _resolve_choices(raw, issues)
    return {
        "rules": chosen,
        "ignore_ids": [] if chosen else [issue["id"] for issue in issues],
        "turn": request.get("turn"),
    }


def _resolve_choices(raw: str, issues: list[dict]) -> list[str]:
    """
    把「1a 3b」这样的选择翻成给 Revision 看的**结论文本**。

    认不出来的部分原样保留——用户直接写判断（"温度系数以 0.07 为准"）也能用。
    """

    import re

    if not raw:
        return []

    by_number = {str(position): issue for position, issue in enumerate(issues, start=1)}
    tokens = [piece for piece in re.split(r"[\s,，、;；]+", raw) if piece]

    resolved: list[str] = []
    leftover: list[str] = []
    used_letters = False
    for token in tokens:
        match = re.fullmatch(r"(\d+)?([a-zA-Z])", token)
        issue = None
        if match:
            index = match.group(1) or ("1" if len(issues) == 1 else "")
            issue = by_number.get(index)
        if issue is None:
            leftover.append(token)
            continue
        options = issue.get("options") or []
        offset = ord(match.group(2).lower()) - ord("a")
        if offset < 0 or offset >= len(options):
            leftover.append(token)
            continue
        used_letters = True
        where = issue.get("span_label") or issue.get("location") or f"第 {match.group(1)} 条"
        resolved.append(f"{where}：{options[offset]}")

    # 一个选项都没认出来 → 整句当作用户自己写的判断。
    # 认出了一部分 → 只把没认出来的那截原样附上，不要把整串再塞一遍
    #（否则会多出一条毫无意义的规则，例如"1好 2a 3a"）。
    if not used_letters:
        return [raw]
    if leftover:
        resolved.append(" ".join(leftover))
    return resolved


def ask_request(request: dict) -> dict:
    return dispatch_request(request, {
        "issue_review": ask_issues,
        "patch_review": ask_patches,
        "final_confirm": ask_final_confirm,
        "human_approval": ask_human,
    })


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.setup_checkpoint:
        from app.workflow.graph import setup_checkpointer
        setup_checkpointer()
        print("Checkpoint 数据库初始化完成。")
        return 0

    if args.ping:
        print("当前 LLM 配置：")
        for key, value in describe_configuration().items():
            print(f"  {key}: {value}")
        response = ping()
        print(f"\n连通性检查：OK\n模型返回：{response.text.strip()[:200]}")
        return 0

    if args.show_graph:
        show_graph()
        return 0

    if args.inspect:
        document, loaded = read_input(args)
        show_structure(document, args.file or "（命令行文本）", loaded=loaded)
        return 0

    document = read_document(args)
    if not document.strip():
        raise SystemExit("论文内容为空。")

    logger = None if args.quiet else print
    harness = Harness(
        auto_approve=args.auto_approve,
        auto_approve_constraints=args.constraint,
        logger=logger,
    )
    source_docx = source_docx_path(args)
    if source_docx:
        print(f"[CLI] 改动会写回 Word 副本（保留原文件）：{Path(source_docx).name}")

    if args.auto_approve:
        result = harness.run_to_completion(
            args.request,
            document,
            run_id=args.run_id or None,
            max_iterations=args.max_iterations,
            source_docx=source_docx,
            rules=args.rule,
        )
    else:
        result = harness.start(
            args.request,
            document,
            run_id=args.run_id or None,
            max_iterations=args.max_iterations,
            rules=args.rule,
            source_docx=source_docx,
        )
        rounds = 0
        while result.is_interrupted and rounds < 10:
            rounds += 1
            request = result.pending_request
            payload = ask_request(request)
            result = harness.resume(payload)

        # 多轮会话：这轮结束后还可以继续提要求（例如“把摘要再学术一点”）
        turns = 0
        while result.status == "completed" and turns < 5:
            try:
                follow_up = input(
                    "\n还想继续提要求吗？（例如：把摘要再学术一点；直接回车结束）："
                ).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not follow_up:
                break
            turns += 1
            # 支持在追问里改要求：记住：…… / R1 改成 …… / 取消 R1
            result = harness.follow_up(follow_up)
            while result.is_interrupted:
                request = result.pending_request
                payload = ask_request(request)
                result = harness.resume(payload)

    print("\n========== 运行摘要 ==========")
    for key, value in result.summary().items():
        print(f"  {key}: {value}")

    if result.status == "failed":
        print(f"\n运行失败：{result.error}")
        return 1

    print("\n========== 最终报告 ==========")
    print(result.final_report)

    if result.docx_path:
        print("\n========== Word 副本（可直接在 Word/WPS 里接受或拒绝）==========")
        print(f"  {result.docx_path}")

    trace = harness.trace_summary()
    print("========== Trace 汇总 ==========")
    print(f"  调用次数：{trace['totals']['calls']}")
    print(f"  失败次数：{trace['totals']['failures']}")
    print(f"  重试次数：{trace['totals']['retries']}")
    print(f"  总耗时：{trace['totals']['total_ms']} ms")
    print(f"  Token 总量：{trace['totals']['total_tokens']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
