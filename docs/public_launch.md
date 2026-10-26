# 从本地到公网：当前免费版上线步骤

## 这一版是什么

- 账号、密码、确认密码注册；不接手机、不收款，没有微信/支付宝支付入口。
- 每账号累计 3 次免费机会。成功完成消耗一次；排队/运行/待确认暂占一次，失败或取消释放。
- 同一账号同时提交也不能超过剩余机会；失败重试重新检查。旧内测任务不占新机会。
- 简单注册不是实名验证：一人多个账号仍可能重复领取。默认每 IP 每小时最多 5 次注册尝试。
- 账号至少 3 位、密码至少 6 位，密码经 Argon2 哈希；会话 HttpOnly，写请求校验 CSRF 和来源。
- 每日提交限制与累计免费次数分别生效。当前未实现自助找回密码，管理员命令可重置并撤销旧会话。
- 原稿初始缩放 95%，可调；左侧修改清单独立滚动，右侧固定展示原稿一页。
- 人工修改事项位于原稿预览下方；仍会在最终 Word 批注中提示。

“免费”仅指用户不付费，服务器、模型调用成本仍由运营方承担。

## 1. 准备资源

你已有阿里云账号，可以先准备域名和一台 Linux 服务器，不需要开通短信服务。
建议先用可按月调整的资源，小范围邀请 5 人验收，再决定长期配置。

初始容量估算：Ubuntu 24.04 LTS，4–8 vCPU / 16 GB RAM、100 GB SSD。
这是针对 5 个后台进程加文档转换/分页的工程起点，不是实测服务器性能保证，也不是购买报价。
长论文、图片较多或同时转换会增加内存和处理时间。上线前须用你的代表性论文压测；机器不足时先降并发。

如果用中国内地服务器，先确认域名、主体、服务器满足备案条件并完成备案后再公开服务。
阿里云说明，解析到中国内地服务器的网站需完成 ICP 备案；是否涉及其他资质应按实际业务向平台确认。
参考：[阿里云 ICP 备案说明](https://help.aliyun.com/zh/icp-filing/basic-icp-service/product-overview/what-is-an-icp-filing)。

## 2. 安装与传代码

1. 安全组只公开 TCP 80/443；SSH 22 仅允许你的管理 IP。不要开放 5432、6379、8000、8080、9000、9001。
2. 按 [Docker Ubuntu 安装文档](https://docs.docker.com/engine/install/ubuntu/) 安装 Docker Engine 和 Compose 插件。
3. 把项目放在 `/opt/paper_revision_agent`。不要把本机 `.venv`、`node_modules`、临时测试文件搬过去。
4. 不要把 `.env`、部署密码或论文上传到公开代码仓库；在服务器使用受限权限的环境文件。

## 3. 配置环境

在项目根目录创建模型配置 `.env`（安全编辑，不把密钥贴到聊天里）：

```dotenv
DEEPSEEK_API_KEY=在服务器填写实际密钥
PRA_MODEL=deepseek-flash
```

首次部署先构建镜像，再用容器生成随机部署密码，不要求宿主机装 Python 依赖：

```bash
docker build -f deploy/Dockerfile -t paper-revision-setup .
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD/deploy:/app/deploy" paper-revision-setup python -m app.web.manage init-env
chmod 600 .env deploy/.env
```

已有 `deploy/.env` 则保留原文件，不能重生成数据库密码。编辑其中这些字段（域名换成你的）：

```dotenv
PRA_DOMAIN=paper.example.com
PRA_PUBLIC_ORIGIN=https://paper.example.com
PRA_COOKIE_SECURE=1
PRA_REGISTRATION_OPEN=1
PRA_FREE_RUNS=3
PRA_WORKER_CONCURRENCY=5
PRA_AUDIT_CONCURRENCY=3
PRA_LLM_CONCURRENCY=15
PRA_WEB_DEMO=0
```

`PRA_FREE_RUNS` 是累计限制，不是每日限制；调整它会作用于全部账号。
保留已有随机数据库、Redis、MinIO 和管理员密码。初次生成时管理员账号密码在此文件。
模型名配置在根目录 `.env`，只有 Worker 获取模型密钥。

## 4. 域名、HTTPS、启动

1. 域名添加 A 记录指向服务器公网 IP。未配置 IPv6 就不要保留错误的 AAAA 记录。
2. 确认域名已解析、安全组 80/443 开放，服务器没有其他程序占用这两个端口。
3. 执行：

```bash
docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml up --build -d
docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml ps
```

新增的 Caddy 入口会自动申请/续期 HTTPS，并把 HTTP 跳转到 HTTPS；这些条件和行为见
[Caddy 官方文档](https://caddyserver.com/docs/automatic-https)。没有域名/正确 DNS 时无法验证真实证书签发。
访问 `https://你的域名`，无需在链接后写 8080。

代理采用专用 `172.29.251.0/24` Docker 网段。先检查它不与服务器已有网络冲突；
如需修改，必须同步修改 `compose.public.yml` 的网段/IP 和 `trusted-proxy.conf`。
内层 nginx 仅信任固定 HTTPS 入口容器转发的客户端地址，避免注册限流被伪造请求头绕过。
不要额外将 API 直接公开到互联网；不要把任意地址加入可信代理。
如日后加 CDN，需要单独配置并验证可信代理链，不能直接信任任意 X-Forwarded-For。

## 5. 正式邀请用户前的验收

- 自己注册一个普通账号，确认只有 3 次机会、看不到工程用量。
- 上传样稿 → 口径选择 → 80% 分页确认 → 下载 Word；拒绝一处修改后核对它未被写入。
- 5 个不同账号同时处理代表性论文，检查 CPU、内存、任务完成时间与模型服务限流。
- 用户 A 的任务链接、预览、下载和确认请求，用户 B 都不能访问。
- 测试同账号多窗口提交：最多占用 3 次；失败/取消释放，已完成不会因点取消恢复次数。
- 重启 API/Worker 后，原有待确认任务仍能继续。
- 保留服务端预算、防滥用限制；排队超出执行数是正常现象，人工等待不会占处理进程。

本地隔离测试验证业务并发和账户隔离，不等同于云端真实论文压力测试。

## 6. 运维和数据保护

- 定期备份 PostgreSQL、MinIO 文件、workspace 文档及环境配置，并实际做一次恢复演练。
- 不要运行 `docker compose down -v`，它会删除持久化卷。
- 升级前先备份；在无运行任务的窗口更新，迁移会保留现有账号/论文。
- 报错先检查 `docker compose ... logs --tail 100 api worker dispatcher`，日志分享前去除个人信息和密钥。
- 需要暂关注册：设置 `PRA_REGISTRATION_OPEN=0`，重新 `up -d`；已有用户仍可登录。
- 忘记密码：`docker compose --env-file deploy/.env -f deploy/compose.yml exec api python -m app.web.manage reset-password 账号名`。
- 上线前提供真实的服务说明、隐私告知、论文保存期限、删除/投诉联系渠道；不要声称论文永不发送第三方模型。
- 当前文档转换与 Worker 在同容器，并非严格隔离的不可信文档沙箱。公开放量前还需安全加固、备份恢复及压测，不能把“能部署”当作完整生产安全验收。

本文提供部署步骤，不代表读者的服务器已完成验证。代码更新和回退见 [更新说明](updating.md)。
