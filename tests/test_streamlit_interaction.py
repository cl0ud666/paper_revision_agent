"""通过 Streamlit AppTest 验证实际组件和 Workflow 闸门闭环。"""

from __future__ import annotations

import importlib.util
import json
import unittest

from app.utils.llm import set_llm_factory
from tests.fakes import fake_factory
from tests.test_deployment_readiness import DeploymentTestCase
from tests.test_service import DOC


@unittest.skipUnless(
    importlib.util.find_spec("streamlit"), "安装 requirements-extras.txt 后运行 Streamlit UI 测试"
)
class TestStreamlitGates(DeploymentTestCase):
    def app(self, request):
        from streamlit.testing.v1 import AppTest

        source = (
            "import streamlit as st\n"
            "from app.ui.streamlit_app import show_request\n"
            f"payload = show_request({request!r})\n"
            "if payload is not None: st.json(payload)\n"
        )
        return AppTest.from_string(source, default_timeout=30).run()

    def test_final_confirm_can_submit_without_issues_and_exclude_group(self):
        request = {
            "type": "final_confirm",
            "run_id": "ui-final",
            "turn": 1,
            "patches": [
                {
                    "patch_id": "patch-001",
                    "patch_ids": ["patch-001", "patch-002"],
                    "count": 2,
                    "replace": "MOCO",
                    "rationale": "统一口径",
                }
            ],
        }
        app = self.app(request)
        self.assertEqual(len(app.exception), 0)
        app.multiselect[0].set_value(["patch-001"])
        app.button[0].click().run()
        self.assertEqual(json.loads(app.json[0].value)["excluded_ids"], ["patch-001"])

    def test_patch_review_can_accept_subset_without_issues(self):
        app = self.app(
            {
                "type": "patch_review",
                "run_id": "ui-patch",
                "iteration": 1,
                "patches": [
                    {"patch_id": "patch-001", "status": "proposed"},
                    {"patch_id": "patch-002", "status": "proposed"},
                ],
            }
        )
        app.multiselect[0].set_value(["patch-002"])
        app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(json.loads(app.json[0].value)["accepted_ids"], ["patch-002"])

    def test_issue_gate_renders_options_and_returns_structured_choice(self):
        app = self.app(
            {
                "type": "issue_review",
                "run_id": "ui-choice",
                "turn": 1,
                "iteration": 1,
                "issues": [
                    {"id": 7, "problem": "大小写统一", "options": ["统一为 MOCO", "统一为 moco"]}
                ],
            }
        )
        app.radio[0].set_value(1)
        app.button[0].click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(json.loads(app.json[0].value)["choices"], {"7": 1})

    def test_main_manual_flow_reaches_final_confirm_then_completes(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string(
            "from app.ui.streamlit_app import main\nmain()", default_timeout=30
        )
        app.session_state["document"] = DOC
        app.run()
        start = next(button for button in app.button if button.label == "开始修改")
        start.click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["result"].pending_request["type"], "final_confirm")
        next(button for button in app.button if button.label == "确认并生成文件").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["result"].status, "completed")

    def test_main_auto_checkbox_runs_to_completion(self):
        from streamlit.testing.v1 import AppTest

        app = AppTest.from_string(
            "from app.ui.streamlit_app import main\nmain()", default_timeout=30
        )
        app.session_state["document"] = DOC
        app.run()
        app.sidebar.checkbox[0].check().run()
        next(button for button in app.button if button.label == "开始修改").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["result"].status, "completed")

    def test_main_terminology_choice_then_final_confirm(self):
        from streamlit.testing.v1 import AppTest

        set_llm_factory(fake_factory({"*": {}}))
        app = AppTest.from_string(
            "from app.ui.streamlit_app import main\nmain()", default_timeout=30
        )
        app.session_state["document"] = "# 正文\n\nMoCo 与 MOCO 均用于实验。"
        app.run()
        next(button for button in app.button if button.label == "开始修改").click().run()
        self.assertEqual(app.session_state["result"].pending_request["type"], "issue_review")
        app.radio[0].set_value(0)
        next(button for button in app.button if button.label == "确认选择并继续").click().run()
        self.assertEqual(len(app.exception), 0)
        self.assertEqual(app.session_state["result"].pending_request["type"], "final_confirm")
        next(button for button in app.button if button.label == "确认并生成文件").click().run()
        self.assertEqual(app.session_state["result"].status, "completed")
