# 参与 Forma

欢迎为非商业目的修复问题、改善交互、增加测试和分享模型评测。项目由 `zhujy9413-commits` 维护，开发过程中使用 Codex 协助实现与验证。

## 许可与贡献范围

允许非商业使用、修改和分享；分发时保留版权、许可及来源声明。商业使用不在许可范围内；请勿通过 PR 移除非商业限制或引入你无权以本许可提供的内容。完整条款以 LICENSE 为准。

## 提交改进

1. Fork 本仓库到自己的 GitHub 账号。
2. 克隆自己的副本，并从 `main` 新建分支，例如 `fix/frame-history`。
3. 修改并验证，提交清晰的 commit。
4. 将分支推送到自己的副本，再向本仓库 `main` 发起 Pull Request。
5. 在 PR 中写明解决的问题、修改原因、验证方法及仍存在的限制。界面变化可附截图，模型效果变化请区分真实结果与模拟结果。

普通贡献者不需要仓库写入权限。维护者会查看代码差异、讨论和验证结果，决定合并、请求修改或仅参考思路。主仓库更新不代表承诺接受所有 PR。

## 本地运行

按 [README](README.md) 启动。真实模型调用使用你自己的 API Key，并由你的账号承担费用；不需要维护者密钥。多数逻辑改动可通过下面的模拟检查验证，无需真实调用。

开发检查需要 Python 3 和支持全局 Blob 的 Node.js（建议 Node.js 18 或以上）。在仓库根目录执行：

```sh
node --check forma/core.js
node --check forma/api.js
node --check forma/app.js
node tests/workflow.cjs
node tests/backup.cjs
python3 tests/backend.py
```

这些检查使用内存夹具、假响应及临时目录，不向百炼发起请求，也不读取本人 `.env`。后端检查会复制当前 `server.py` 到临时目录，避免改动你的 `.runtime`。

文档修改通常只需检查内容、链接与排版。流程修改应运行相关检查；新增行为请提供有意义的回归验证。不要为了提交 PR 反复调用真实模型。

## 贡献约定

- 生成成功不等于内容合格。保留失败案例，不把模拟状态或少量样例写成普遍质量结论。
- 不提交 `.env`、`.runtime/`、个人项目备份、真实凭证或他人未授权素材。
- 不引入自动付费重试；改变任务数量、上传范围或采用策略时，保持明确提示及用户选择。
- 尽量保持改动聚焦。保留原素材、已采用版本与编辑结果。
- 代码和文档的贡献遵循 [Forma 非商业源码使用许可 1.0](LICENSE)，不自动转让贡献者版权；新增图片和视频请标明来源、服务条款及可授予的使用范围，见 [NOTICE](NOTICE.md)。

## 跟进主仓库更新

Fork 用户可使用 GitHub 的 **Sync fork** 更新副本，也可给本地副本添加上游：

```sh
git remote add upstream https://github.com/zhujy9413-commits/forma-ai-studio.git
git fetch upstream
git switch main
git merge --ff-only upstream/main
```

若你已直接修改 `main`，快进合并可能失败。请先保存自己的提交，再按 Git 的提示合并或将改动放到独立分支；不要直接重置丢弃自己的工作。
