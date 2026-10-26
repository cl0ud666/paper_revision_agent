# PaperFixer

面向研究者的论文修改智能体。上传 Word 论文后，后台完成审查和修订；需要作者决定的口径先确认，最终在原稿分页预览中逐项选择，再下载带修订痕迹和批注的 DOCX。

技术栈：React / TypeScript、FastAPI、Celery、Redis、PostgreSQL、LangGraph。模型通过 OpenAI 兼容接口接入，默认配置面向 DeepSeek。

## 使用流程

1. 注册或登录，上传 DOCX、DOC、WPS、RTF 或 ODT，填写修改目标。
2. 后台按段落审查，再进行跨章节对照；有歧义的术语和事实口径由作者选择。
3. 在原稿分页预览中查看原文、拟修改内容和原因，取消不需要的修改。
4. 下载修订版 Word。无法自动完成的事项保留原因与人工修改说明。

默认最多两类用户确认：口径选择、最终写入确认。没有待决定的口径时直接进入修订。代码内容不参与自动文本替换；没有实验数据时不会生成看似真实的实验结果。

## 能力与边界

| 能力 | 当前行为 |
| --- | --- |
| 文字修订 | 按段落及原文片段定位，输出 Word 修订痕迹和批注 |
| 术语统一 | 结合上下文识别概念，由用户确认写法；不按词频直接判错 |
| 格式修改 | 支持字体、字号及部分段落格式，生成可确认的结构补丁 |
| 原稿预览 | LibreOffice 转换与 PDF 分页渲染；不能保证与所有 Word 版本完全一致 |
| 公式 | 可转换部分 LaTeX 为原生 OMML；复杂对象仍可能需要人工处理 |
| 新增表格、引用 | 当前产品策略关闭自动新增表格/行列、引用和书目，返回人工处理事项 |
| 旧格式 | 后台转换为 DOCX；WPS 仅支持 Word 兼容格式，复杂版式可能变化 |

模型审查不是事实正确性的保证。补丁的定位、用户选择、写回状态由程序验证；专业判断仍需要作者核对。删除 Word 批注不等于拒绝修订。

## 本地启动

推荐 Python 3.12、Node.js 22，以及启用 Linux 容器的 Docker Compose。

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate

python -m pip install -r requirements.txt -r requirements-web.txt -r requirements-dev.txt
```

复制根目录 `.env.example` 为 `.env`，填写自己的模型 API Key。不要把它提交到 Git。

```bash
python -m app.web.manage init-env
docker compose --env-file deploy/.env -f deploy/compose.yml up --build -d
```

访问 [本地工作台](http://localhost:8080)。管理员账号及初始密码由 `init-env` 写入 `deploy/.env`；已有配置不会覆盖。首次初始化后，修改环境变量不会重置已有账号密码，重置命令见[部署说明](docs/web_deployment.md)。

默认每个普通账号累计三次免费任务，失败或取消会释放机会；同时有每日提交上限。并发数可以配置，但实际容量取决于机器内存、文件转换和模型接口配额。

仅演示流程、不调用真实模型时，可使用 [演示配置](deploy/compose.demo.yml)；它不能验证模型修改质量。

## 项目结构

```text
app/
  agents/       审查、修订、复核和报告；审查材料构建与结果验证独立成模块
  document/     段落定位、补丁、原稿预览、DOCX 写回和操作能力限制
  workflow/     LangGraph 节点、恢复机制、交互协议及人工输入规范化
  web/          HTTP 接口、用户/任务事务、队列、后台执行与文件存储
  service/      CLI / Streamlit 使用的本机运行管理器
  state/        工作流状态结构
  utils/        模型调用、预算、上下文、重试和追踪
frontend/src/
  pages/        任务列表、任务详情和账号管理
  components/   上传、人工确认及通用状态组件
  features/review/  分页审阅、布局联动和人工修改事项
migrations/     业务数据库迁移
tests/          离线 Python 回归测试
frontend/e2e/   浏览器交互测试
evals/          评测集、指标和评测运行器
deploy/         Docker 镜像、Compose 与反向代理
scripts/        打包、仓库检查和集成验证工具
```

处理任务时，API 先把任务和待投递记录写入 PostgreSQL，Dispatcher 再投递 Celery。Worker 运行到人工确认节点便保存 Checkpoint 并释放执行资源；提交选择后由下一段任务恢复，不占用进程等待用户点击。

设计细节与关键约束见[架构说明](docs/architecture.md)。

## 开发与验证

Python 测试使用假模型，不需要真实 API Key。PostgreSQL、LibreOffice 和真实浏览器/账号等集成验收有单独的环境要求，见[贡献指南](CONTRIBUTING.md)。

```bash
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

GitHub Actions 对每次 push / pull request 执行上述离线检查；UI 测试启动独立开发服务，接口采用测试数据，不连接线上账号。

## 部署与更新

- [本地与服务器部署](docs/web_deployment.md)
- [域名、HTTPS 与公网发布](docs/public_launch.md)
- [源码更新与回退](docs/updating.md)
- [GitHub 上传检查](docs/github.md)
- [文档目录](docs/README.md)

不要把运行目录、客户论文、密钥或旧部署压缩包提交到仓库。生产数据保存在数据库及文件存储卷中；更新源码和重建容器不会自动重新处理旧任务。

## 许可证

尚未指定开源许可证。公开展示代码与授权第三方使用、修改、分发是不同的决定；正式开放复用前，由项目所有者选择并添加许可证。
