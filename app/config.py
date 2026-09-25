"""
全局配置中心。

设计意图：

1. 所有可调参数（模型、超时、重试、熔断、Context 预算、路径）集中在一处；
2. 通过 ``.env`` / 环境变量覆盖，代码里不写死；
3. 其他模块只通过 :func:`get_settings` 读取，测试时可 :func:`reset_settings` 重置。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/ 的上一层）
PROJECT_ROOT = Path(__file__).resolve().parents[1]

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
    # Audit 之后是否先问一句"这些问题有没有要忽略的"（默认开）。
    issue_gate: bool
    # 审查范围：body（摘要+正文，默认）/ body+refs / all
    # 封面、目录、诚信承诺、致谢这些默认不审，避免把排版内容当论文问题。
    audit_scope: str


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
        return self.runs_dir / run_id


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
            # 默认开启：Audit 之后先问一句“这些问题有没有要忽略的”，
            # 并把用户的决定持久记下来（多轮不再重复问）。
            issue_gate=_get_bool("PRA_ISSUE_GATE", True),
            audit_scope=_get("PRA_AUDIT_SCOPE", "body"),
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
