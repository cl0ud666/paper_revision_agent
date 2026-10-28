"""
学术论文智能修改Multi-Agent系统。

模块划分：

- ``app.config``全局配置（模型 / 超时 / 重试 / 熔断 / Context预算 / 路径）
- ``app.state``     Workflow的共享State（PaperState）
- ``app.agents``    4个Agent + Report（Orchestrator / Audit / Revision / Review）
- ``app.workflow``  LangGraph Workflow、Router、Human-in-the-loop节点与Harness
- ``app.utils``     LLM封装、JSON解析、上下文治理、重试熔断、Trace
- ``app.ui``        Streamlit界面
- ``evals``评测闭环（Golden Set + LLM-as-Judge + 回归）
"""

__version__ = "2.0.0"
