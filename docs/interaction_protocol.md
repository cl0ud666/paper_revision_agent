# 人工交互协议与 PostgreSQL Checkpoint

部署时安装 `requirements.txt` 和 `requirements-web.txt`，在环境变量设置：

```dotenv
PRA_CHECKPOINT_BACKEND=postgres
PRA_CHECKPOINT_URL=postgresql://paper_agent:password@localhost:5432/paper_agent
PRA_CHECKPOINT_POOL_SIZE=5
```

然后执行一次 `python main.py --setup-checkpoint` 创建/升级 Checkpoint 表。
普通任务只使用连接池，不重复执行建表迁移。PostgreSQL 连接失败会明确报错，不回退内存。
`auto` 后端有 URL 时选 PostgreSQL，无 URL 时保留本地 SQLite；`memory` 仅供测试。
既有 SQLite 暂停任务应先用 SQLite 完成；切换数据库不会自动迁移旧 Checkpoint。

`PostgresSaver` 的连接池按进程创建，Celery Worker 应在子进程中创建 Harness。
LangGraph 内部表由 Checkpointer 管理；目前 `RunManager` 的业务状态仍是本地 JSON，
接 FastAPI/Celery 时需把任务归属、状态、待决策请求迁移到 PostgreSQL 业务表。

## 请求和回答

所有入口依赖 `app.workflow.interaction`，用 `dispatch_request()` 按 `request.type` 选择界面，
用 `prepare_response()` 校验并转换回答。Harness.resume() 从真实 Checkpoint 读取请求再次校验。
未知 type 会明确报错。回答包含 turn/iteration 时必须匹配当前请求，避免旧页面误提交。

| type | 内容 | 回答 |
| --- | --- | --- |
| issue_review | issues，包含 id、problem、options（字符串列表） | choices 为 `{issue_id: option_index}`；可加 rules 或 ignore_ids |
| patch_review | patches，包含 patch_id、status、before、replace、rationale | accepted_ids / rejected_ids / accept_all / reject_all |
| final_confirm | patches；分组项有 patch_ids、count | excluded_ids / exclude_all |
| human_approval | 兼容旧版 issues 和 defaults | decision、approved_issue_ids、constraints |

例如选择第 7 个问题的第二个选项：

```json
{"choices": {"7": 1}, "turn": 1, "iteration": 1}
```

`option_index` 从 0 开始。服务端从已保存的 options 获取选项文本，写入 user_rules；
不依赖前端提交的显示文案。Streamlit 显示“先不修改”和每个可选口径；CLI 保留 `1a 3b` 输入。

最终确认支持补丁 ID 或显示序号。统一术语的分组项保存全部成员 patch_ids，
剔除一组会展开成所有成员 ID，防止合并展示后的序号错位。
最终 excluded_patch_ids 会返回 LangGraph State，进入 Checkpoint 和 state.json，
后续轮次默认保留先前剔除结果。此确认控制 Word 导出；工作中的内存文本仍用于复核。

## 暂停和恢复

RunSession 在进入 awaiting_human 前，原样保存完整 pending_request 到 session.json。
重启后直接恢复，不根据 proposed_patches 或 audit_findings 猜暂停类型。
旧版 session.json 缺少请求时读取实际 Checkpoint interrupt；两者均不存在时明确拒绝恢复。
session/state 使用同目录临时文件加原子替换，避免读到不完整 JSON。

未来 Celery 的任务运行到 interrupt 后应保存请求并结束，释放 Worker；
用户回答通过鉴权 API 保存后，投递新任务，以相同 thread_id 调用 Harness.resume(payload)。
不要让 Worker 阻塞等待用户。当前 RunManager semaphore 只限制该管理器内的后台线程，
不能代替多进程、多机器的 Celery 配额和 Redis 全局 LLM 限流。

## 运行 ID 和并发

run_id 限制为安全的单个目录名，并检查解析后目录必须位于 runs 根目录内。
同名任务不得覆盖已有产物；start 创建原子 .started 标记，resume/follow_up 复用现有任务。
评测任务每次生成新 run_id，避免历史结果被覆盖。
RunManager 在真正启动 start/resume/follow_up 时获取并发名额，暂停/结束/异常时释放。

## 验证

离线回归：

```bash
python -m pytest -q
```

Streamlit AppTest 需要 `requirements-extras.txt` 中的 Streamlit。
要验证真实 PostgreSQL 重启恢复，配置专用测试库（测试会建 Checkpoint 表并写入运行记录）：

```powershell
$env:PRA_TEST_POSTGRES_URL = "postgresql://paper_agent:password@localhost:5432/paper_agent_test"
python -m pytest tests/test_deployment_readiness.py -k RealPostgres -q
```

未提供专用测试库时这条集成测试会跳过；其余 PostgreSQL 适配测试使用模拟连接池。
