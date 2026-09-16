# 可重复邮件处理工作流

## 用途与边界

本工作流处理需要回复的邮件，并保留从收信、分类、资料检索、人工补充、草稿编辑、经验应用、冻结确认、SMTP 发送到归档的完整证据。邮件正文、附件和模型输出均是不可信数据，不能创建长期规则、扩大个人资料读取范围、确认发送或触发 ehall 提交。

经验规则只有在用户从一次明确纠正或自己保存的草稿版本主动提出、逐字段审核并批准后才生效。`task` 范围补充只属于当前任务；`personal` 范围补充才进入长期个人资料。规则文件不保存邮件全文、邮箱地址和个人资料。

## 长期运行入口

```bash
# 安装当前服务模板、重启并验证本机受保护接口
./lab1/restart_and_verify.sh

# 人工执行一轮，或查看/控制调度器
python3 lab1/mail_scheduler.py once
python3 lab1/mail_scheduler.py status
python3 lab1/mail_scheduler.py pause
python3 lab1/mail_scheduler.py resume

# 查看经验生命周期；这些命令不会扫描或发送邮件
python3 lab1/experience.py proposals
python3 lab1/experience.py rules
python3 lab1/experience.py recover
```

手机网页默认由 `gse-mail-web.service` 提供。访问口令位于被 Git 忽略的 `data/web-token.txt`；HTTP 只适用于可信本机或可信局域网，公网使用不在本实验范围。

## 每封邮件的状态序列

1. 调度器用 IMAP UID 和 UIDVALIDITY 增量采集，保存原始邮件与结构化快照，不改变邮箱已读标志。
2. 分类器只读取当前邮件及有可验证回复头的有限往来，按 `mail.classify` 捕获活动规则。失败、依据不足或规则协议错误不会降级成 `no_reply`。
3. `reply_required` 创建或复用唯一邮件任务；`no_reply` 不创建任务；`user_review` 等待用户决定。重复扫描同一来源只复用原分类、来源绑定和任务。
4. 任务规划按 `mail.plan` 冻结规则快照，将每项个人事实拆成有引用的查询。用户决定和 `task` 补充只作用于当前任务。
5. 拟稿按 `mail.draft` 使用同一任务冻结的版本，并保存逐条 `rule_results`。适用规则必须引用当前任务中的逐字片段。
6. 用户可保存草稿新版本。普通编辑不会自动学习；只有在历史版本中点击“从此修改提出可复用规则”，填写推广说明并另行批准，才会发布长期规则。
7. 发送前生成包含收件人、主题、正文、附件哈希、资料依赖和版本号的冻结预览。用户必须确认该精确指纹；任何编辑、资料变化或往来更新都会使旧预览失效。
8. SMTP 在进入结果可能不明的 DATA 阶段前写入持久状态。`accepted` 只表示服务器接收；`unknown` 禁止自动重发。
9. 只有存在 `accepted` 记录的任务可以归档。归档只影响列表可见性，不删除来源、草稿历史、确认版本、发送结果或经验应用审计。

## 经验审核与跨会话复用

- 待审核草案、启用规则和停用规则在手机页分区展示，批准、拒绝、停用和恢复均为单独操作。
- 新分类或新任务从 `experience/rules/mail/` 捕获当前活动版本；进程重启不改变规则身份和摘要。
- 任务详情通过 `GET /api/tasks/<任务ID>/experience` 获取该任务自己的规则快照和应用证据，不返回其他任务邮件或个人资料。
- 发布新版本或停用规则不会修改历史任务；显式“按当前决定和最新经验重新规划”才更新未完成任务的规则快照。

## 故障与恢复

- 模型、网络或快照错误：保持 `retry`、`failed` 或 `needs_review`，不猜测成功。
- 网页关闭或进程重启：从 SQLite、邮件快照和规则文件继续；不依赖浏览器内存。
- SMTP 在 DATA 后中断：恢复为 `unknown`，只允许只读核对，不自动重发。
- 规则发布中断：运行 `python3 lab1/experience.py recover`；若检测到人工文件冲突则停止覆盖。
- 新相关往来：旧草稿保留，用户明确选择纳入或忽略后才重新准备。

## 隔离验收

```bash
PYTHONPATH=lab1 python3 -m unittest -v lab1.test_stage5_e
PYTHONPATH=lab1 python3 -m unittest discover -s lab1 -p 'test_*.py'
```

隔离验收使用合成邮箱、模型响应、SMTP 传输和 ehall 页面，不连接真实外部服务。它必须证明重复扫描不增加任务或发送次数，重启后仍可查询来源、草稿、冻结发送内容、SMTP 结果和经验应用记录；ehall 的第二个合成适配器也必须经过识别、结构化检查、决定规范化、试填回读、后果展示、精确确认、单次提交与结果核对边界。

真实验收仅在用户主动提供测试邮件并逐次确认时进行。不得为了测试自动发送邮件、自动创建 ehall 任务或点击真实提交按钮。
