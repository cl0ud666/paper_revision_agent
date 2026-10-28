# Agent与结构编辑更新（2026-10-19）

## 已实现

- 建议类问题也进入补丁生成；能落实的修改进入同一最终确认清单。不能执行的实验、缺失资料、定位/写回失败明确保留为人工事项，不静默消失。
- 注册密码最低6位；修改注册页标题与字号；移除网页查看/下载修改报告入口及空的未完成提示；最终文件标题旁增加Word批注查看说明。
- 增加字体字号/粗斜体、段落样式/对齐/间距、规则表格编辑、LaTeX转原生OMML公式、Word原生引用域与书目数据源。
- 结构补丁带操作参数、原文锚点与源文件校验值，写前预检，最终确认后才应用；用户取消则从原始文件重放其余补丁。
- 每处补丁合并一条批注；确认后写回失败同时进入网页最终框和Word批注。代码、已有公式/图片、复杂域不通过暴力全文重写来修改。
- 文献允许模型提出候选；新增候选用Crossref独立核对标题、作者、年份、DOI，保留来源供确认。原文或用户已提供的资料另行标记为“未联网核验”，不混同为第三方验证。
- 数字引用一致性审计覆盖缺失条目、未引用条目、编号重复、首次引用顺序。只诊断，不凭空补数据或自动删除文献。
- Docker增加libreoffice-math，避免Word有公式但服务器预览不显示。

## 能力边界

- 书目信息核验不等于读过全文、证实论据；Crossref未收录或网络失败不代表文献虚假。未能核验的新文献返回人工事项。
- 表格支持单元格文字、规则表格行列增删、横向合并/拆分、边框底纹列宽。复杂纵向合并、嵌套、含特殊对象或已有修订的结构操作会拒绝并说明原因。表格多页/定位歧义不会强行写入。
- 单元格文字、增删行、格式、公式/引用插入带相应原生修订；列拓扑和合并/拆分不承诺Word内逐项“拒绝修订”还原，确认卡会提示。所有操作均能在网页最终确认阶段取消，并从原稿重放。
- 公式插在明确原文锚点前后，不覆盖已有公式/图片。不支持的LaTeX不转成乱码或假称成功；复杂公式需核对显示。
- 新增Word CITATION/BIBLIOGRAPHY与数据源已实现；不承诺Zotero/Mendeley同步、不混改其域、不自动迁移所有旧引用。当前新建书目采用作者—年份缓存显示，不保证直接得到GB/T 7714数字编号；Word更新域后的样式需按学校要求核对。
- 删除批注不等于撤销修订；需要在网页取消该补丁或使用Word的拒绝修订。原稿始终保留。
- 已完成/正在确认的旧任务不会自动重新生成候选补丁，升级后用新任务检验。

## 验证

Python回归为467项通过、1项跳过，另23个子测试通过；前端生产构建成功；浏览器交互回归9项通过。另在部署用Linux镜像内进行了DOCX集成验证。
集成样本覆盖格式、原生公式、表格、引用四类修改，四条合并批注、全部取消、确认后故障回报，以及原图片/样式/原公式保护。
DOCX已重开并经LibreOffice渲染逐页检查；未使用Microsoft Word桌面程序验证每一种F9/拒绝修订行为，不把XML测试称作Word全版本兼容保证。

## 更新服务器

这是已部署版本的更新包，不是首次部署包。只包含应用源码、前端构建源码/配置、依赖和Dockerfile；不包含或覆盖`.env`、`deploy/.env`、Compose配置、数据库、客户论文、存储卷。

1. 等待已有任务结束，暂时不要提交新任务。保留当前Workbench会话。
2. 把`paperfixer-update-20261019-agent.zip`上传到服务器`/opt/`。
3. 在Workbench执行下列命令，先备份源码，再覆盖更新文件。备份文件名带时间，方便恢复。

```bash
cd /opt/paper_revision_agent
tar --exclude='frontend/node_modules' --exclude='frontend/dist' --exclude='frontend/test-results' --exclude='__pycache__' -czf "/opt/paperfixer-code-before-$(date +%Y%m%d-%H%M%S).tgz" app frontend requirements.txt requirements-web.txt deploy/Dockerfile
unzip -o /opt/paperfixer-update-20261019-agent.zip -d /opt/paper_revision_agent
COMPOSE_PARALLEL_LIMIT=1 docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml build api worker dispatcher web
```

若`unzip`命令不存在，先安装：`apt-get update && apt-get install -y unzip`。
构建成功后才执行下一步；构建失败不要先停旧容器。2核4 GB使用串行构建以减少内存峰值。

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml up -d --no-deps api worker dispatcher web
docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml ps
curl -I https://paperfixer.online
```

4. 刷新网页，用新的小测试文档检查注册、确认和下载。不要只看HTTP 200就认为所有任务正常。
5. 如果需要回退，解压上面实际生成的源码备份、重新构建/启动这四个服务；保留源码备份和旧部署包。

不要执行`docker compose down -v`、删除PostgreSQL/Redis/MinIO卷或上传本地密钥。此次更新不要求购买额外服务或变更数据库结构。

## 防火墙与运行费用

保持80/443可访问。22的`0.0.0.0/0`建议改为必要来源，但不能直接删掉唯一管理通道：Workbench可能使用SSH 22。先按阿里云Workbench的来源要求加窄规则，验证新的连接成功，再删全网规则。数据库/Redis/内部接口不对公网开放。
网站需要随时访问时服务器应保持运行，本地电脑可关机；包月时段不会因关机暂停。模型API、套餐外资源按其各自规则计费。
