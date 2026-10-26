"""显式启用的部署验收模式；不访问真实模型，不用于学术质量评估。"""

import json

from app.agents.prompts import ORCHESTRATOR_HEADER, REVIEW_HEADER, REVISION_HEADER
from app.utils.llm import set_llm_factory


def install_demo():
    class Model:
        def invoke(self, messages):
            prompt = "\n".join(str(m[1]) for m in messages)
            if ORCHESTRATOR_HEADER in prompt:
                data = {
                    "plan_kind": "full_review",
                    "next_action": "audit",
                    "plan": ["审查论文"],
                    "focus_areas": ["术语使用"],
                    "reason": "演示流程",
                    "instructions": "按用户选择统一术语",
                }
            elif REVISION_HEADER in prompt:
                data = {"patches": [], "notes": "演示模式只执行确定性术语统一"}
            elif REVIEW_HEADER in prompt:
                data = {
                    "status": "pass",
                    "summary": "演示模式：确定性术语统一流程完成，未验证学术质量。",
                    "per_issue": [],
                    "remaining_issues": [],
                    "constraint_violations": [],
                    "dimension_scores": {},
                }
            else:
                data = {"issues": []}

            class Message:
                content = json.dumps(data, ensure_ascii=False)
                usage_metadata = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}

            return Message()

    set_llm_factory(lambda agent, overrides: Model())
