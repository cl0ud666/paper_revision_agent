# 参与开发

先阅读 [README](README.md) 和 [架构说明](docs/architecture.md)。建议使用Python 3.12、Node.js 22；前端依赖使用`npm ci`安装，与锁文件保持一致。

## 安装与检查

```bash
python -m pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
python -m pytest -q
ruff check .
ruff format --check .
python scripts/check_repository.py --history

cd frontend
npm ci
npm run format:check
npm run build
npx playwright install chromium
npm run test:ui
```

修改Python后运行`ruff format`，前端运行`npm run format`。测试目录包含历史unittest和pytest两种写法，统一用pytest收集。真实模型连通性检查使用`python -m scripts.diagnostics.check_llm`，会调用配置的模型并产生费用。

## 验证层次

- Python回归：假模型、临时SQLite和本地存储，覆盖工作流、用户隔离、额度、补丁和Word XML写回，不要求生产密钥。
- 离线UI：`npm run test:ui`自动启动本地Vite，拦截全部API，不需要已有账号或运行中的业务服务。
- 分页预览：`scripts/verify_visual_review.py`创建合成论文并调用LibreOffice，生成的`data/verification-temp/`不提交Git；随后在`frontend`运行`npm run test:preview`，使用独立本地服务验证分页、缩放、定位及勾选提交。
- 真实服务：`frontend/e2e/web.spec.ts`等会创建测试用户和任务，仅对专用测试部署执行；不要默认指向生产网站。
- PostgreSQL集成：`tests/test_deployment_readiness.py`，通过`PRA_TEST_POSTGRES_URL`指定专用测试库，未配置则跳过；不要使用生产数据库。

离线测试不能证明模型学术判断正确，也不能代替Microsoft Word中的实际修订和版式验收。

## 本地诊断

`scripts/diagnostics/`放置显式调用的排查工具，不再混入测试目录：

```bash
python -m scripts.diagnostics.inspect_run <run_id>
python -m scripts.diagnostics.inspect_audit <run_id>
python -m scripts.diagnostics.inspect_structure <file-or-run-dir>
python -m scripts.diagnostics.check_llm
```

前三项只读本地数据；不要公开含论文内容的输出。`check_llm`和`python -m scripts.diagnostics.run_audit_once <file-or-run-dir>`会调用真实模型并产生费用，仅在明确需要时运行。

## 代码约束

- Agent负责提出候选，不直接修改文件；文件写回通过`document`层的补丁校验。
- 不绕过用户确认；用户拒绝的补丁不能在重试、恢复或下一轮里偷偷恢复。
- `operation_policy.py`是产品能力限制的统一入口；新增操作必须同时实现定位、预检、确认、写回和失败反馈。
- HTTP层保持轻量，耗时执行交给Worker；确认等待通过Checkpoint持久化。
- 数据库变更新增Alembic迁移，不能修改已经发布的迁移。
- bug修复增加能重现用户问题的测试；纯格式调整不添加只验证排版的测试。
- 不提交真实论文、运行轨迹、密钥、数据库、压缩包或客户信息。问题报告使用合成样本。

提交说明写清触发条件、改变后的行为、验证方式和限制。暂未选择开源许可证，复用授权由项目所有者决定。
