# 上传GitHub

此仓库保留本地开发历史。上传前先完成检查，不需要把服务器上的数据库或论文同步回来。

```bash
python scripts/check_repository.py --history
git status --short
git diff --check
```

检查器扫描当前跟踪文件、未忽略的新文件，以及HEAD、本地分支和标签可达的文本历史。它检查常见令牌、私钥和运行产物路径，输出文件位置而不输出密钥值；不能识别所有敏感信息。正式发布还应检查合成样本之外是否包含真实论文或个人信息。

下列内容由`.gitignore`排除：环境密码、虚拟环境、前端依赖、构建结果、论文运行目录、检查点、验证产物和发布压缩包。`.gitignore`不能移除已经进入提交历史的内容。如果发现历史密钥，先吊销/轮换，再处理历史。

在GitHub新建空仓库，按实际用户名和仓库名设置远程地址。如果本地已有`origin`，先核对它，不要直接覆盖。

```bash
git remote -v
git add .
git diff --cached --stat
git diff --cached --check
git commit -m "Refactor paper_revision_agent for maintainability and public review"
git remote add origin https://github.com/YOUR_ACCOUNT/paper_revision_agent.git
git push -u origin main
```

只推送明确的分支。不要用`git push --mirror`；它可能把编辑器本地恢复快照也上传。GitHub Actions会运行离线测试、格式检查和前端构建，首次云端运行需要单独确认结果。

当前没有选择许可证。如果目标是面试展示，可以先公开展示代码；如果要允许其他人自由复用，由项目所有者选择并添加LICENSE。不要把API Key、管理员密码写到仓库描述、Issue或截图里。
