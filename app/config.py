"""
全局配置中心。

设计意图：

1. 所有可调参数（模型、超时、重试、熔断、Context 预算、路径）集中在一处；
2. 通过 ``.env`` / 环境变量覆盖，代码里不写死；
3. 其他模块只通过 :func:`get_settings` 读取，测试时可 :func:`reset_settings` 重置。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一层）
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def validate_run_id(run_id: str) -> str:
    """运行 ID 同时用作目录名，只允许单个安全路径组件。"""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", run_id):
        raise ValueError("run_id 必须为 1–128 位字母、数字、下划线、连字符或点，且以字母数字开头")
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        run_id.endswith(".")
        or run_id.split(".")[0].upper() in reserved
        or run_id.lower() == "evals"
    ):
        raise ValueError("run_id 不能使用保留目录名")
    return run_id


# 读取项目根目录下的 .env（文件不存在时静默跳过）
load_dotenv(PROJECT_ROOT / ".env")


def _get(name: str, default: str) -> str:
    value = os.getenv(name)
    return value if value not in (None, "") else default


def _get_int(name: str, default: int) -> int:
    try:
        return int(float(_get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _get_float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = _get(name, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class LLMSettings:
    """LLM 连接与默认生成参数。"""

    model: str
    base_url: str
    api_key_env: str
    temperature: float
    max_tokens: int
    timeout: float
    json_mode: bool
    log_calls: bool
    log_prompt_chars: int

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env)


@dataclass(frozen=True)
class AgentSettings:
    """每个 Agent 的生成参数（温度越低越稳定）。"""

    orchestrator_temperature: float
    audit_temperature: float
    revision_temperature: float
    review_temperature: float

    def temperature_for(self, agent: str) -> float:
        mapping = {
            "orchestrator": self.orchestrator_temperature,
            "audit": self.audit_temperature,
            "revision": self.revision_temperature,
            "review": self.review_temperature,
        }
        return mapping.get(agent, 0.2)


@dataclass(frozen=True)
class RetrySettings:
    """指数退避重试策略。"""

    max_attempts: int
    base_delay: float
    max_delay: float
    jitter: float


@dataclass(frozen=True)
class CircuitSettings:
    """熔断器参数（每个 Agent 独立统计）。"""

    failure_threshold: int
    recovery_timeout: float


@dataclass(frozen=True)
class ContextSettings:
    """
    Context 治理预算。

    把长论文一次性塞给 LLM 是 Context Window 爆炸的主要原因，
    因此这里为每个 Agent 单独设置预算。
    """

    orchestrator_budget: int
    audit_budget: int
    revision_budget: int
    review_budget: int
    audit_chunk_tokens: int
    audit_chunk_overlap: int
    audit_max_chunks: int
    # Audit 精度控制
    audit_min_severity: str  # high / medium / low（默认 medium：丢掉"可改可不改"的）
    audit_max_issues: int  # 合并后的问题上限
    # 专名核验：把全文专有名词清单单独交给模型核一遍拼写与领域惯例
    # （"阿里云 ESC"这种，正确写法 ECS 本来就不需要出现在文中）
    audit_name_check: bool
    # Audit 分块并发度：分块之间没有依赖，可以并行跑。
    # 1 = 退回串行；上限还受分块总数约束（不会为 3 个分块开 6 个线程）。
    audit_concurrency: int
    # 跨批对齐：所有分块跑完后，把全部分块的全部发现摆到一起再过一次，
    # 合并"说的是同一件事"的重复项（分块之间互相看不到，只能事后对齐）。
    audit_cross_check: bool
    # 定向对照：把"要互相对照的两份材料"摆到一起查矛盾
    # （摘要×结论、承诺×产出、正文数字×表格）。
    # 比整篇送审便宜一个数量级，而且对照任务本来就该把两份材料放一起。
    audit_targeted: bool
    # 定向对照的 Context 预算。0 = 不截断（对照材料本来就很短）。
    audit_targeted_tokens: int
    # 「正文数字 × 表格」这一组最多取多少句正文数字句。
    # 超出会被丢弃 —— 丢弃数量会打印出来，不会静默。
    # 0 = 不限（全量送）。注意别设成很小的值，否则关键数值句会被挤掉。
    audit_compare_sentences: int
    # 整篇兜底核查：只在"小文档"或"其它环节一条问题都没发现"时才跑。
    audit_whole_paper: bool
    # 全文矛盾核查的 Context 预算。
    # 0 = 不限；默认给一个上限，避免长论文把整次调用撑爆（它没有分块兜底）。
    audit_whole_paper_tokens: int
    # 整篇送审的"小文档"门槛（估算 token）：整篇不超过它就直接送。
    audit_whole_paper_small_doc: int
    # 跨批对齐的 Context 预算。0 = 不限：候选清单全部送进去（默认）。
    audit_cross_check_tokens: int
    revision_max_tokens: int
    review_max_tokens: int
    prompt_head_ratio: float
    max_issues: int

    def budget_for(self, agent: str) -> int:
        mapping = {
            "orchestrator": self.orchestrator_budget,
            "audit": self.audit_budget,
            "revision": self.revision_budget,
            "review": self.review_budget,
        }
        return mapping.get(agent, self.audit_budget)


@dataclass(frozen=True)
class WorkflowSettings:
    """Workflow 生命周期治理参数。"""

    max_iterations: int
    recursion_limit: int
    checkpoint_db: str
    agent_timeout: float
    # 兼容字段：实际只对 decision findings 进入人工拍板闸门（默认关）。
    issue_gate: bool
    # 补丁审阅闸门（默认**关**）：
    #   False = 不打断用户，Revision 报出「原文／准备修改为／原因」后直接应用；
    #   True  = 恢复成逐条接受/拒绝。
    # 关掉它的理由：需要人拍板的"前后矛盾"已经在 Audit 之后问过一次，
    # 再让用户逐条勾一边补丁属于重复劳动。
    patch_gate: bool
    # 写入 Word 之前的最后一次确认（默认**开**）。
    # 前面每轮都在内存里改文本，Word 只在最后写一次，所以确认也只需一次：
    # 把所有轮次的改动一起列出来，用户剔除掉的那几条不写进 Word。
    final_confirm: bool
    # 审查范围：body（摘要+正文，默认）/ body+refs / all
    # 封面、目录、诚信承诺、致谢这些默认不审，避免把排版内容当论文问题。
    audit_scope: str
    checkpoint_backend: str = "auto"
    checkpoint_url: str = ""
    checkpoint_pool_size: int = 5


@dataclass(frozen=True)
class BudgetSettings:
    """
    成本与延迟预算。

    超预算时不是直接崩，而是**降级**：
    Audit 不再开新的分块、Revision 不再开新的批次、Review 直接收敛出报告。
    """

    max_run_tokens: int  # 整个任务（含多轮）的 token 上限，0 = 不限
    max_turn_tokens: int  # 单轮上限
    max_run_seconds: float  # 整个任务的墙钟上限
    max_calls_per_agent: int  # 单个 Agent 在整次任务里的调用次数上限


@dataclass(frozen=True)
class ServerSettings:
    """Web 服务的部署参数。"""

    host: str
    port: int
    api_token: str
    cors_origins: tuple[str, ...]
    max_concurrent_runs: int
    event_poll_interval: float

    @property
    def auth_enabled(self) -> bool:
        return bool(self.api_token)


@dataclass(frozen=True)
class OutputSettings:
    """
    交付物设置：把改动写回 Word 原文件（另存副本）。

    ``docx_output`` 关掉时只产出 Markdown，不生成 .docx；
    上传的如果本来就不是 Word（pdf/txt/md），这一路会自动跳过。
    """

    docx_output: bool
    redline_author: str
    redline_comments: bool


@dataclass(frozen=True)
class PathSettings:
    """项目内的路径。"""

    project_root: Path
    data_dir: Path
    runs_dir: Path
    samples_dir: Path
    golden_set: Path

    def run_dir(self, run_id: str) -> Path:
        validate_run_id(run_id)
        root = self.runs_dir.resolve()
        target = (root / run_id).resolve()
        if not target.is_relative_to(root) or target == root:
            raise ValueError("运行目录必须位于 runs 根目录内")
        return target


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    agents: AgentSettings
    retry: RetrySettings
    circuit: CircuitSettings
    context: ContextSettings
    workflow: WorkflowSettings
    budget: BudgetSettings
    server: ServerSettings
    output: OutputSettings
    paths: PathSettings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """读取（并缓存）全局配置。"""

    project_root = PROJECT_ROOT

    def _resolve(name: str, default: Path) -> Path:
        path = Path(_get(name, str(default)))
        return path if path.is_absolute() else project_root / path

    data_dir = _resolve("PRA_DATA_DIR", project_root / "data")
    runs_dir = _resolve("PRA_RUNS_DIR", data_dir / "runs")
    samples_dir = _resolve("PRA_SAMPLES_DIR", data_dir / "samples")
    golden_set = _resolve("PRA_GOLDEN_SET", data_dir / "golden" / "golden_set.jsonl")

    return Settings(
        llm=LLMSettings(
            model=_get("PRA_MODEL", "deepseek-chat"),
            base_url=_get("PRA_BASE_URL", "https://api.deepseek.com"),
            api_key_env=_get("PRA_API_KEY_ENV", "DEEPSEEK_API_KEY"),
            temperature=_get_float("PRA_TEMPERATURE", 0.2),
            max_tokens=_get_int("PRA_MAX_TOKENS", 4096),
            timeout=_get_float("PRA_TIMEOUT", 120.0),
            json_mode=_get_bool("PRA_JSON_MODE", True),
            # 每次模型调用的完整 Prompt 与返回都落盘，方便回查“Agent 到底看到了什么”
            log_calls=_get_bool("PRA_LOG_LLM", True),
            # 0 = 不截断
            log_prompt_chars=_get_int("PRA_LOG_PROMPT_CHARS", 0),
        ),
        agents=AgentSettings(
            orchestrator_temperature=_get_float("PRA_TEMP_ORCHESTRATOR", 0.0),
            audit_temperature=_get_float("PRA_TEMP_AUDIT", 0.2),
            revision_temperature=_get_float("PRA_TEMP_REVISION", 0.3),
            review_temperature=_get_float("PRA_TEMP_REVIEW", 0.0),
        ),
        retry=RetrySettings(
            max_attempts=_get_int("PRA_RETRY_MAX_ATTEMPTS", 3),
            base_delay=_get_float("PRA_RETRY_BASE_DELAY", 1.0),
            max_delay=_get_float("PRA_RETRY_MAX_DELAY", 8.0),
            jitter=_get_float("PRA_RETRY_JITTER", 0.25),
        ),
        circuit=CircuitSettings(
            failure_threshold=_get_int("PRA_CIRCUIT_THRESHOLD", 4),
            recovery_timeout=_get_float("PRA_CIRCUIT_RECOVERY", 90.0),
        ),
        context=ContextSettings(
            orchestrator_budget=_get_int("PRA_CTX_ORCHESTRATOR", 2500),
            audit_budget=_get_int("PRA_CTX_AUDIT", 3000),
            revision_budget=_get_int("PRA_CTX_REVISION", 6000),
            review_budget=_get_int("PRA_CTX_REVIEW", 8000),
            audit_chunk_tokens=_get_int("PRA_AUDIT_CHUNK_TOKENS", 1500),
            audit_chunk_overlap=_get_int("PRA_AUDIT_CHUNK_OVERLAP", 120),
            audit_max_chunks=_get_int("PRA_AUDIT_MAX_CHUNKS", 24),
            audit_min_severity=_get("PRA_AUDIT_MIN_SEVERITY", "medium").lower(),
            audit_max_issues=_get_int("PRA_AUDIT_MAX_ISSUES", 15),
            audit_name_check=_get_bool("PRA_AUDIT_NAME_CHECK", True),
            audit_concurrency=_get_int("PRA_AUDIT_CONCURRENCY", 6),
            audit_cross_check=_get_bool("PRA_AUDIT_CROSS_CHECK", True),
            audit_targeted=_get_bool("PRA_AUDIT_TARGETED", True),
            audit_targeted_tokens=_get_int("PRA_AUDIT_TARGETED_TOKENS", 0),
            audit_compare_sentences=_get_int("PRA_AUDIT_COMPARE_SENTENCES", 40),
            audit_whole_paper=_get_bool("PRA_AUDIT_WHOLE_PAPER", True),
            # 默认 24000：超过就按"头尾保留"截断，避免一次调用被撑爆
            audit_whole_paper_tokens=_get_int("PRA_AUDIT_WHOLE_PAPER_TOKENS", 24000),
            # 整篇不超过这个量就无条件送（小文档一次送审最省事）
            # 整篇送审的门槛：正文不超过它就无条件整篇送一次。
            # 你的论文正文约 1.7 万 token，所以这里给到 20000 让它每轮都跑。
            # （整篇是唯一"不依赖我们枚举配对范围"的覆盖，值得这 2 万 token。）
            audit_whole_paper_small_doc=_get_int("PRA_AUDIT_WHOLE_PAPER_SMALL_DOC", 20000),
            audit_cross_check_tokens=_get_int("PRA_AUDIT_CROSS_CHECK_TOKENS", 0),
            revision_max_tokens=_get_int("PRA_REVISION_MAX_TOKENS", 6000),
            review_max_tokens=_get_int("PRA_REVIEW_MAX_TOKENS", 6000),
            prompt_head_ratio=_get_float("PRA_PROMPT_HEAD_RATIO", 0.6),
            max_issues=_get_int("PRA_MAX_ISSUES", 30),
        ),
        workflow=WorkflowSettings(
            max_iterations=_get_int("PRA_MAX_ITERATIONS", 3),
            recursion_limit=_get_int("PRA_RECURSION_LIMIT", 60),
            # 默认把 Checkpoint 存到 SQLite：这样服务重启后，
            # 停在“等待人工确认”的任务还能继续（真正可部署的关键）
            checkpoint_db=_get(
                "PRA_CHECKPOINT_DB", str(data_dir / "checkpoints" / "paper_agent.sqlite")
            ),
            agent_timeout=_get_float("PRA_AGENT_TIMEOUT", 180.0),
            # 保留旧配置入口；当前流程只对 decision findings 进入人工拍板，
            # 普通 fix/advice 直接进入 Revision。
            issue_gate=_get_bool("PRA_ISSUE_GATE", False),
            patch_gate=_get_bool("PRA_PATCH_GATE", False),
            final_confirm=_get_bool("PRA_FINAL_CONFIRM", True),
            audit_scope=_get("PRA_AUDIT_SCOPE", "body"),
            checkpoint_backend=_get("PRA_CHECKPOINT_BACKEND", "auto").lower(),
            checkpoint_url=_get("PRA_CHECKPOINT_URL", ""),
            checkpoint_pool_size=max(1, _get_int("PRA_CHECKPOINT_POOL_SIZE", 5)),
        ),
        budget=BudgetSettings(
            max_run_tokens=_get_int("PRA_BUDGET_RUN_TOKENS", 400000),
            max_turn_tokens=_get_int("PRA_BUDGET_TURN_TOKENS", 150000),
            max_run_seconds=_get_float("PRA_BUDGET_RUN_SECONDS", 1800.0),
            max_calls_per_agent=_get_int("PRA_BUDGET_CALLS_PER_AGENT", 40),
        ),
        server=ServerSettings(
            host=_get("PRA_HOST", "127.0.0.1"),
            port=_get_int("PRA_PORT", 8000),
            api_token=_get("PRA_API_TOKEN", ""),
            cors_origins=tuple(
                origin.strip()
                for origin in _get("PRA_CORS_ORIGINS", "").split(",")
                if origin.strip()
            ),
            max_concurrent_runs=_get_int("PRA_MAX_CONCURRENT_RUNS", 2),
            event_poll_interval=_get_float("PRA_EVENT_POLL_INTERVAL", 1.0),
        ),
        output=OutputSettings(
            # 默认开：用户上传 .docx 时，改动会以 Word 修订痕迹写回一份副本
            docx_output=_get_bool("PRA_DOCX_OUTPUT", True),
            # Word 里显示"谁改的"和"谁写的批注"
            redline_author=_get("PRA_REDLINE_AUTHOR", "论文修改助手"),
            redline_comments=_get_bool("PRA_REDLINE_COMMENTS", True),
        ),
        paths=PathSettings(
            project_root=project_root,
            data_dir=data_dir,
            runs_dir=runs_dir,
            samples_dir=samples_dir,
            golden_set=golden_set,
        ),
    )


def reset_settings() -> None:
    """清空配置缓存（测试用）。"""

    get_settings.cache_clear()
