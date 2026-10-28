# 人工交互协议与PostgreSQL Checkpoint

部署时安装`requirements.txt`和`requirements-web.txt`，在环境变量设置：

```dotenv
PRA_CHECKPOINT_BACKEND=postgres
PRA_CHECKPOINT_URL=postgresql://paper_agent:password@localhost:5432/paper_agent
PRA_CHECKPOINT_POOL_SIZE=5
```

然后执行一次`python main.py --setup-checkpoint`创建/升级Checkpoint表。
普通任务只使用连接池，不重复执行建表迁移。PostgreSQL连接失败会明确报错，不回退内存。
`auto`后端有URL时选PostgreSQL，无URL时保留本地SQLite；`memory`仅供测试。
既有SQLite暂停任务应先用SQLite完成；切换数据库不会自动迁移旧Checkpoint。

`PostgresSaver`的连接池按进程创建，Celery Worker应在子进程中创建Harness。
LangGraph内部表由Checkpointer管理；目前`RunManager`的业务状态仍是本地JSON，
接FastAPI/Celery时需把任务归属、状态、待决策请求迁移到PostgreSQL业务表。

## 请求和回答

所有入口依赖`app.workflow.interaction`，用`dispatch_request()`按`request.type`选择界面，
用`prepare_response()`校验并转换回答。Harness.resume() 从真实Checkpoint读取请求再次校验。
未知type会明确报错。回答包含turn/iteration时必须匹配当前请求，避免旧页面误提交。

| type | 内容 | 回答 |
| --- | --- | --- |
| issue_review | issues，包含id、problem、options（字符串列表） | choices为`{issue_id: option_index}`；可加rules或ignore_ids |
| patch_review | patches，包含patch_id、status、before、replace、rationale | accepted_ids / rejected_ids / accept_all / reject_all |
| final_confirm | patches；分组项有patch_ids、count | excluded_ids / exclude_all |
| human_approval | 兼容旧版issues和defaults | decision、approved_issue_ids、constraints |

例如选择第7个问题的第二个选项：

```json
{"choices": {"7": 1}, "turn": 1, "iteration": 1}
```

`option_index`从0开始。服务端从已保存的options获取选项文本，写入user_rules；
不依赖前端提交的显示文案。Streamlit显示“先不修改”和每个可选口径；CLI保留`1a 3b`输入。

最终确认支持补丁ID或显示序号。统一术语的分组项保存全部成员patch_ids，
剔除一组会展开成所有成员ID，防止合并展示后的序号错位。
最终excluded_patch_ids会返回LangGraph State，进入Checkpoint和state.json，
后续轮次默认保留先前剔除结果。此确认控制Word导出；工作中的内存文本仍用于复核。

## 暂停和恢复

RunSession在进入awaiting_human前，原样保存完整pending_request到session.json。
重启后直接恢复，不根据proposed_patches或audit_findings猜暂停类型。
旧版session.json缺少请求时读取实际Checkpoint interrupt；两者均不存在时明确拒绝恢复。
session/state使用同目录临时文件加原子替换，避免读到不完整JSON。

未来Celery的任务运行到interrupt后应保存请求并结束，释放Worker；
用户回答通过鉴权API保存后，投递新任务，以相同thread_id调用Harness.resume(payload)。
不要让Worker阻塞等待用户。当前RunManager semaphore只限制该管理器内的后台线程，
不能代替多进程、多机器的Celery配额和Redis全局LLM限流。

## 运行ID和并发

run_id限制为安全的单个目录名，并检查解析后目录必须位于runs根目录内。
同名任务不得覆盖已有产物；start创建原子 .started标记，resume/follow_up复用现有任务。
评测任务每次生成新run_id，避免历史结果被覆盖。
RunManager在真正启动start/resume/follow_up时获取并发名额，暂停/结束/异常时释放。

## 验证

离线回归：

```bash
python -m pytest -q
```

Streamlit AppTest需要`requirements-extras.txt`中的Streamlit。
要验证真实PostgreSQL重启恢复，配置专用测试库（测试会建Checkpoint表并写入运行记录）：

```powershell
$env:PRA_TEST_POSTGRES_URL = "postgresql://paper_agent:password@localhost:5432/paper_agent_test"
python -m pytest tests/test_deployment_readiness.py -k RealPostgres -q
```

未提供专用测试库时这条集成测试会跳过；其余PostgreSQL适配测试使用模拟连接池。
