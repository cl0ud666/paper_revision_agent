# 网页版启动与部署

已实现账号密码自行注册、Word 类文件上传、后台任务、术语选择、最终确认和 Word 下载。网页不提供修改报告入口；报告仍作为内部诊断产物保留。
当前方案：每账号累计 3 次免费机会，暂不收费、不接手机号验证。
从零购买资源及发布请看 [上线步骤](public_launch.md)。
前端为 React + TypeScript + Vite；API 为 FastAPI；Celery 使用同步 prefork Worker。
PostgreSQL 同时保存业务表和 LangGraph Checkpoint；Redis 用作 Broker 和全局模型限流。
所有下载都经过用户归属校验，文件桶不公开。

## 本机启动

需要 Docker Desktop 使用 Linux 容器，或 Linux 上的 Docker Compose。
在仓库根目录执行：

```powershell
python -m pip install -r requirements.txt -r requirements-web.txt
python -m app.web.manage init-env
docker compose --env-file deploy/.env -f deploy/compose.yml up --build -d
```

已有 `.env` 文件中配置平台的 `DEEPSEEK_API_KEY`。只有 Worker 通过 env_file 接收模型密钥，
密钥和部署密码不会打包进入镜像或返回网页。
`init-env` 随机生成部署密码，已有 `deploy/.env` 时会拒绝覆盖。
管理员账号和初始密码在 `deploy/.env` 的 `PRA_ADMIN_USERNAME`、`PRA_ADMIN_PASSWORD`。

访问 http://localhost:8080 。默认只绑定本机回环地址。
新用户可在登录页自行注册；管理员可在“用户管理”创建账号、调整每日限制和停用账号。
忘记密码时由服务器管理员执行以下命令，交互输入新密码；该账号的旧会话会被撤销：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yml exec api python -m app.web.manage reset-password 账号名
```

默认每账号累计 3 次免费机会，排队/运行/等待选择占用机会，成功完成后计入已使用；失败或取消释放。
重试会重新原子检查剩余次数；取消请求尚未执行完毕时仍占用。历史内测任务不占新额度。
另有每日 5 次提交的防滥用限制（按 UTC+8 日期计数），失败/取消不会重置每日提交次数。
模型内部每篇预算默认 400,000 token，只在服务端管理，网页和用户报告不显示其数量。
模型调用会先按预估预留额度，成功后按实际返回的用量结算；未返回用量或异常时按保守预留量结算。
等待人工选择不计入后台处理时限。

如需先验证流程而不调用付费模型：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.demo.yml up --build -d
```

网页会显示“演示模式”。此模式使用离线模型，只自动生成确定性的术语统一补丁，
不能评估真实论文的学术质量。切回真实模型：

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yml up -d api worker
```

演示验收生成的报告会保留演示说明，不会被当作真实模型的审查结果。

## 任务与恢复机制

### 输入格式与转换限制

网页接受 `.docx`、`.doc`、`.wps`、`.rtf`、`.odt`，不是仅放开扩展名：

- DOCX 直接处理；内容实际为 DOCX 的 DOC/WPS 不转换、不损失布局。
- Word 97 类 DOC、RTF、ODT 在 Worker 内用 LibreOffice 转为 DOCX。
- WPS 只支持内部为 Word OOXML、Word OLE 或 RTF 的文件；不承诺识别所有旧式金山专有格式。
- 加密、宏、结构异常、超限文件拒绝；转换限时 120 秒。RTF 嵌入对象暂不支持。
- 输出统一 DOCX，不回转旧格式。复杂排版、公式、字体、分页可能改变，用户需对照原稿。
- 上传原件不可变；另存规范化 DOCX 到私有存储，人工闸门恢复/重试复用它，不重新转换。
- 全部剔除时返回无本次修改的规范化 DOCX，而不是把原始 DOC/RTF 改扩展名后下载。

Dockerfile 已声明 LibreOffice Writer 和中文字体，升级需重新构建镜像。
非容器运行需安装 LibreOffice，并将 `soffice` 放进 PATH 或设置 `PRA_LIBREOFFICE` 为可执行文件绝对路径；
安装 `requirements-web.txt` 中的 `olefile` 用于检测旧式 DOC。每次转换使用独立临时用户配置，禁止宏。
生产外网部署还应把不可信文件转换放进有内存/CPU/网络限制的独立沙箱；临时 profile 和超时并不等于安全沙箱。
本次格式扩展接入网页上传/Worker；旧 CLI、Streamlit 的格式入口尚未统一迁移，不能据此宣称所有入口都支持 DOC/WPS。

### 批注与撤销

每处已落实修改只生成一条解释批注：`将「原文」改为「新文」。原因：……`。
同一插入内容再次修改时更新原文到最终结果的说明；整段重写会合并本次生成的旧修订，
不删除作者原有的批注。回到原文的连续修改不再留下该项修订。

Word 的原生替换仍由删除和插入两个修订组成，显示多少气泡由编辑器控制，
不能通过普通 DOCX 强制 Word/WPS 在点某一个 × 时同时撤销两者。
删除批注不等于撤销修改。应同时拒绝对应的删除和插入，或者在网页“最终确认”中整项剔除后导出。
这里没有把修订改成普通红字，也没有伪造相同 revision ID 来假装原子撤销。

参考：[微软修订说明](https://support.microsoft.com/en-gb/word/training/track-changes-in-word?ad=gb&rs=en-gb&ui=en-us)、
[LibreOffice 命令行转换说明](https://help.libreoffice.org/latest/en-GB/text/shared/guide/start_parameters.html?DbPAR=BASIC&System=WIN)。

### 执行与恢复

上传完成后，API 在 PostgreSQL 中创建 run 和 job，返回 HTTP 202。
独立 `dispatcher` 扫描待投递 job 并发送到 Celery；Redis 断线时任务仍保存在数据库中。
已投递但没开始执行的 job 会重新投递，执行中的 job 有定时续期的租约。

Worker 获取论文级 PostgreSQL advisory lock 后执行，重复投递不会重复修改论文。
每个执行片段还带 execution_token，旧执行不能覆盖新执行的业务状态和额度。
Compose 默认 5 个 Celery 执行进程，每个审查任务内部最多 3 路，全局最多 15 个模型调用。
分别用 `PRA_WORKER_CONCURRENCY`、`PRA_AUDIT_CONCURRENCY`、`PRA_LLM_CONCURRENCY` 配置。
等待人工选择不占 Worker 执行进程。5 人可同时处理；超出执行名额的任务排队，不会互相覆盖。

遇到人工请求时，Worker 将完整 pending_request 和请求 ID 原样写入数据库，随后结束该片段。
用户回答经过身份、归属、请求 ID、版本和选项校验；回答与新的恢复 job 在同一个事务中保存。
重复提交相同回答返回当前状态，提交不同回答返回 409。
恢复片段使用同一 thread_id 读取 Checkpoint；如 Checkpoint 已推进到新闸门，
恢复时会展示真正的新闸门，不会把旧答案再次提交。

用户可取消排队、等待人工或执行中的任务。执行中的任务在当前模型调用返回后停止，
不会承诺立刻终止已经发出的网络请求。失败任务可以从最后保存的位置重试，沿用已有 token 额度。
没有改动或最终全部剔除时，仍返回保留原文的 Word 文件和报告。

## 源码位置

| 模块 | 位置 |
| --- | --- |
| API 与账号鉴权 | `app/web/api.py`、`service.py` |
| ORM 与迁移 | `app/web/models.py`、`migrations/` |
| Celery 与恢复 | `app/web/worker.py`、`queue.py` |
| 持久化任务投递 | `app/web/dispatcher.py` |
| 文件存储 | `app/web/storage.py` |
| 网页 | `frontend/src/` |
| 容器与网关 | `deploy/` |

API 路由使用 async def，通过线程池调用同步业务事务，避免阻塞 API 的事件循环。
数据库连接在业务函数/Worker 内创建，不跨进程传递 Session、文件对象或 Harness 实例。
旧 RunManager、CLI 和 Streamlit 仍可用于本地调试；网页任务以 PostgreSQL 业务表为准。

## 验证

```powershell
python -m pip install -r requirements-extras.txt
python -m pytest -q
cd frontend
npm ci
npm run build
npm run test:e2e
```

浏览器测试需先启动演示 Compose 环境，并使用 `deploy/.env` 中的管理员账号创建专用测试账号。
测试后会停用测试账号。测试会重启此项目的 Worker、短暂停止并恢复 Redis，验证恢复逻辑。
Windows 上优先使用已安装的 Edge；其他环境可执行 `npx playwright install chromium`。
可用 `PRA_E2E_URL` 和 `PRA_E2E_BROWSER_CHANNEL` 指定目标地址和浏览器。

## 运维

### 分页最终确认

新建网页任务启用原稿分页审阅，最多“口径选择 + 最终确认”两次交互。镜像需包含
LibreOffice Writer、中文字体和 `requirements-web.txt` 中的 `pdfplumber`。
预览存入私有对象存储，由带登录权限校验的页面接口读取；不会暴露文件桶。
升级前的已创建任务保留原协议，新体验需新建任务。详细行为、边界和验收见 [visual_review.md](visual_review.md)。

```powershell
docker compose --env-file deploy/.env -f deploy/compose.yml ps
docker compose --env-file deploy/.env -f deploy/compose.yml logs --tail 100 api worker dispatcher bootstrap
docker compose --env-file deploy/.env -f deploy/compose.yml stop
```

`stop` 不删除数据卷。更新代码后用 `up --build -d`，bootstrap 会执行 Alembic 和 Checkpoint 迁移，
已有管理员的密码不会被启动过程覆盖。
数据库、文件桶和工作目录必须一起备份；不要使用 `down -v` 清理持有用户数据的环境。
当前不会自动删除用户论文，文件留存期限需要在生产运营时制定。

首次公网部署在 Linux 上进行：配置正式域名、HTTPS，
设置 `PRA_PUBLIC_ORIGIN=https://正式域名`、`PRA_COOKIE_SECURE=1`，由反向代理转发到本机 8080。
数据库和 Redis 保持内网访问，平台 API Key 由服务器环境注入。

## MinIO 的当前限制

MinIO 社区仓库于 2026-04-25 归档，上游公开镜像已不可用。
本项目的 MinIO 容器从固定源码版本 `RELEASE.2025-10-15T17-29-55Z` 构建，包含上游 LICENSE，
仅用于本地开发与验收。[上游仓库](https://github.com/minio/minio)、[固定版本说明](https://github.com/minio/minio/releases/tag/RELEASE.2025-10-15T17-29-55Z)。

正式生产推荐配置受维护的 S3 服务或云对象存储；使用兼容 S3 的 endpoint、bucket、region、access key 和 secret key 即可。
存储配置为 `PRA_S3_ENDPOINT`、`PRA_S3_BUCKET`、`PRA_S3_REGION`、`PRA_S3_ACCESS_KEY`、`PRA_S3_SECRET_KEY`。
Compose 默认值指向本地 MinIO，生产部署需替换这些值并移除开发用 MinIO 服务。
