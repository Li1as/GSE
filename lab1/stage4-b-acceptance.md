# 阶段 4B 验收记录

验收日期：2026-09-16。

## 实现边界

`ehall_tasks.py` 在现有 `tasks.sqlite` 中新增独立的 `ehall_tasks`
父任务表，保存完整规范 URL、对外脱敏 URL、任务级简要描述、
可选附件元数据、页面结构摘要、字段版本、资料子任务和用户决定。
父任务不伪装成邮件任务，也不创建 ehall 轮询器。

`ehall_adapters/timetable_withdrawal.py` 是“我的课表”退课事务的显式
4B 字段适配器。它只接受结构化只读快照，校验页面身份、课程稳定编号和
可退选状态，并把“退哪门课”建模为必须由用户回答的当前任务决定。
即使简要描述中已写课程名，也不会据此自动选课。4B 验收时适配器尚无
`fill` 或 `submit` 接口，因而当时无法填写或提交真实页面；后续 4C
只增加了不触发页面事件的控件绑定与回读，仍无 `submit`。

个人事实字段继续由阶段一 `Assistant` 创建独立子任务，其
`completed`、`waiting_input`、`needs_review` 和 `failed` 状态投影回
ehall 父任务。补充回复必须属于该父任务的子任务；`task` 范围只在当前
子任务中复用，`personal` 范围才会按阶段一规则归档并供后续任务查询。

## 离线验收

新增 `test_ehall_tasks.py` 共 6 项测试，使用虚构课程和合成表单快照，
未读取真实课表内容：

- URL、简要描述和附件哈希可跨进程重建持久保留，对外 URL 不含 query 和 fragment。
- 简要描述不代替具体课程选择；不可退课程值被拒绝，有效明确选择后才进入 `ready_to_fill`。
- 页面身份错误使父任务保持为 `needs_review` 并记录 `PAGE_CHANGED`；无可退选课程同样不继续准备。
- 合成个人资料字段能进入缺失追问，合格补充后恢复同一父任务。
- `task` 补充不泄漏给新父任务，`personal` 补充可按用户选择供后续父任务使用。
- 附件名路径穿越和非法哈希被拒绝。

执行命令：

```bash
PYTHONPATH=lab1 python3 -m unittest -v lab1.test_ehall_tasks
PYTHONPATH=lab1 python3 -m unittest discover -s lab1 -p 'test_*.py'
```

结果：新增 6 项测试全部通过；Lab1 全量 190 项测试全部通过。
含 HTTP 本机端口的测试在允许本机监听的隔离环境中执行。

## 仍未实现

4B 没有新的真实站点操作。课表 DOM 到结构化课程快照的真实选择器、
手机创建/追问/编辑界面、附件字节管理、浏览器试填与回读均属于 4C。
冻结预览、确认、单次提交、`unknown` 保护和结果核对属于 4D。
在这些机制完成前，不会点击真实退课控件。
