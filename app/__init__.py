"""
学术论文智能修改 Multi-Agent 系统。

模块划分：

- ``app.config``    全局配置（模型 / 超时 / 重试 / 熔断 / Context 预算 / 路径）
- ``app.state``     Workflow 的共享 State（PaperState）
- ``app.agents``    4 个 Agent + Report（Orchestrator / Audit / Revision / Review）
- ``app.workflow``  LangGraph Workflow、Router、Human-in-the-loop 节点与 Harness
- ``app.utils``     LLM 封装、JSON 解析、上下文治理、重试熔断、Trace
- ``app.ui``        Streamlit 界面
- ``evals``         评测闭环（Golden Set + LLM-as-Judge + 回归）
"""

__version__ = "2.0.0"
