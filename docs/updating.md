# 更新服务器上的应用

适用于已经通过`deploy/compose.yml`和`deploy/compose.public.yml`运行的Linux部署。先完成本地回归和一次新任务验收，再发布；已完成任务不会因更新自动重跑。

## 打包和上传

在本地仓库根目录运行：

```bash
python scripts/check_repository.py
python scripts/package_agent_update.py
```

输出包在`artifacts/`，命令显示实际文件名及SHA-256。包含源码、前端配置、Dockerfile、Nginx配置和数据库迁移；不包含环境密钥、Compose配置、论文或数据库。Compose有变更时应单独对照修改，不能用本地配置覆盖线上环境。

使用Workbench上传到自己有写入权限的目录，例如`/home/admin/`。以下命令里的`UPDATE.zip`替换成实际上传的文件名。

## 备份

在无运行任务的维护窗口操作。先暂停新提交，确认任务不会在切换时继续执行。

```bash
cd /opt/paper_revision_agent
sudo install -d -m 700 /opt/paper_revision_agent-backups
sudo tar --exclude='frontend/node_modules' --exclude='frontend/dist' \
  --exclude='__pycache__' --exclude='frontend/test-results' \
  -czf "/opt/paper_revision_agent-backups/code-$(date +%Y%m%d-%H%M%S).tgz" \
  app frontend migrations alembic.ini main.py requirements.txt requirements-web.txt deploy
```

备份含部署密码，因此备份目录只允许root访问。迁移前单独备份PostgreSQL；下面通过同一个root shell写入受限目录：

```bash
sudo sh -c 'umask 077; docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml exec -T postgres pg_dump -U paper_agent -d paper_agent -Fc > "/opt/paper_revision_agent-backups/database-$(date +%Y%m%d-%H%M%S).dump"'
```

另外按部署实际卷名备份MinIO和workspace。数据库备份不能代替文件备份，也不能只备份源码就声称能恢复全部用户数据。

## 构建与切换

```bash
sudo unzip -o /home/admin/UPDATE.zip -d /opt/paper_revision_agent
sudo env COMPOSE_PARALLEL_LIMIT=1 docker compose --env-file deploy/.env \
  -f deploy/compose.yml -f deploy/compose.public.yml build bootstrap api worker dispatcher web
```

构建失败时停止后续步骤，旧容器仍运行旧镜像。构建成功且任务已排空后：

```bash
sudo docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml stop api worker dispatcher web
sudo docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml run --rm --no-deps bootstrap
```

确认迁移命令成功退出后再启动：

```bash
sudo docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml up -d --no-deps api worker dispatcher web
sudo docker compose --env-file deploy/.env -f deploy/compose.yml -f deploy/compose.public.yml ps
```

检查健康状态、登录、上传、两类确认及下载；用合成小论文验证，不只看首页HTTP 200。

## 回退

保留旧镜像和上述备份，验证新版本前不要清理镜像。没有数据库结构变化时，可从对应源码备份恢复受影响文件并重新构建。涉及迁移时先判断旧代码是否兼容新结构，必要时在维护窗口恢复数据库和相应文件备份；不要盲目回滚迁移。

不要执行`docker compose down -v`，它会删除持久化卷。更换管理员密码使用`python -m app.web.manage reset-password admin`管理命令，单独修改`.env`不会重置已有账号密码。
