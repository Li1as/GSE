# 生成式软件工程（2026 秋）讲义归档

来源：[课程主页](https://jyywiki.cn/GSE/2026/)；首次归档：2026-09-14；最近核对：2026-11-16。当前归档共 12 讲、12 份对应幻灯片及 Lab1、Lab2。第 13 讲目前只有标题，没有资料链接。

本次新增第 4–12 讲及 Lab2，详见 [更新内容与考核说明](update-2026-11-16.md)。讲义和实验以 Markdown 保存；网站返回渲染页面，新增正文按页面结构还原为 Markdown，并非原始源码的逐字节副本。幻灯片保存 HTML 原件。来源与文件校验值见 [归档清单](archive-manifest.json)。

## 文件索引

| 讲次 | 主题 | 讲义 | 幻灯片 |
| --- | --- | --- | --- |
| 1 | 欢迎来到未来 | [Markdown](01-welcome-to-the-future.md) | [HTML](slides/01-welcome-to-the-future.html) |
| 2 | 提示词与上下文工程 | [Markdown](02-prompt-and-context-engineering.md) | [HTML](slides/02-prompt-and-context-engineering.html) |
| 3 | 版本管理 (1) | [Markdown](03-version-control.md) | [HTML](slides/03-version-control.html) |
| 4 | 版本管理 (2) | [Markdown](04-version-control-2.md) | [HTML](slides/04-version-control-2.html) |
| 5 | 软件工程的来龙去脉 | [Markdown](05-software-engineering-history.md) | [HTML](slides/05-software-engineering-history.html) |
| 6 | 需求和架构 (1) | [Markdown](06-requirements-and-architecture-1.md) | [HTML](slides/06-requirements-and-architecture-1.html) |
| 7 | 需求和架构 (2) | [Markdown](07-requirements-and-architecture-2.md) | [HTML](slides/07-requirements-and-architecture-2.html) |
| 8 | Agent Native 开发 | [Markdown](08-agent-native-development.md) | [HTML](slides/08-agent-native-development.html) |
| 9 | DeepSeek Harness | [Markdown](09-deepseek-harness.md) | [HTML](slides/09-deepseek-harness.html) |
| 10 | 软件的演化 | [Markdown](10-software-evolution.md) | [HTML](slides/10-software-evolution.html) |
| 11 | 软件质量保障 (1) | [Markdown](11-software-quality-assurance-1.md) | [HTML](slides/11-software-quality-assurance-1.html) |
| 12 | 软件质量保障 (2) | [Markdown](12-software-quality-assurance-2.md) | [HTML](slides/12-software-quality-assurance-2.html) |

实验讲义：[Lab1：会成长的个人助手](lab1-growing-personal-assistant.md)（配图：[ai-native.webp](ai-native.webp)）；[Lab2：Deeper Research](lab2-deeper-research.md)（配图：[bot.webp](bot.webp)、[search.webp](search.webp)）。

## 讲义内容摘要

1. **欢迎来到未来**：讨论 AI 降低软件实现成本后，软件工程仍需解决的核心问题：把模糊意图变成可验证的规格与实现，并用拆解、测试、反馈和迭代避免项目陷入难维护的“泥潭”。介绍 Agent 作为“模型 + 工具 + 环境反馈循环”，并强调人要负责问题选择、设计判断、验收和责任承担。
2. **提示词与上下文工程**：将 prompt 扩展为完整上下文：系统指令、工具说明、项目规则、技能、代码、历史和工具输出都会影响 Agent。重点是明确完成标准、区分指令与材料、保留短而稳定的规则、把可验证要求写入测试或检查，并用不同探索路径和有效反馈来推进不确定任务。
3. **版本管理**：从工作区、暂存区、提交、分支、合并和冲突讲解 Git 的快照模型；进一步说明 `.gitignore`、可读提交信息、原子变更、测试/CI、PR 和协作流程。面向 Agent 开发，主张先让架构与约束收敛，再以可独立验证、可回退的小变更推进，并讨论高产能 AI 下变更组合与并发控制的挑战。

## Lab1：会成长的个人助手

Lab1 是**建议性实验，不计分**。目标是做一个可持续服务个人、且能沉淀经验的助手；技术栈、模型和交互方式不限，可复用已有的 Agent、脚本和技能。

需要具备五项基础能力：

1. **个人数据库**：索引和检索个人资料；资料变化后更新索引；回答能定位原文。课程推荐以 Markdown 与目录组织，以便人工检查和修改记忆。
2. **持续关联 smail 邮箱**：获取新邮件、关联往来历史并结合资料生成回复草稿；邮件只能发送用户确认后的最终版本，重复轮询不得重复处理或发送。
3. **办理 ehall 事务**：在 `ehall.nju.edu.cn` 查询信息，并至少完成一种事务的材料准备与表单填写。提交前必须展示关键字段和后果，用户明确确认后才可提交；退课、撤销申请等不可自行决定。
4. **手机端联动**：可以从手机发起任务、看进度、修改草稿并确认操作；不依赖某个长期保持打开的桌面对话窗口。
5. **能力组合**：五类能力共享必要上下文，并实现至少一条自己愿意反复使用的端到端流程。

还要把成功流程和用户纠正沉淀成可读、可改的脚本、技能或记忆，并用 Git 管理实现与规则。课程给出的示例流程是：收到办事通知后检索资料、整理材料、在手机追问、准备 ehall 表单和邮件草稿，用户核对提交/发送后再归档结果。

讲义将此实验定位为练习需求工程与可维护性：不要只做一次性可运行的 Agent，而要把可验收的行为、人工确认、可恢复流程与能跨会话复用的经验组织起来。

## Lab2 与课程考核

Lab2 在已有个人资料检索基础上增加可控制预算的 `effort`，通过探索、查证、比较和修改提高结果质量；建议比较直接求解、低 effort 和高 effort 的质量、耗时及 token 消耗。Lab2 同样是**建议性实验，不计分**，未完成 Lab1 也可以开始。

第 1 讲幻灯片的课程政策是尽量采用“通过 / 不通过”，“不验收作业，不评分”，Token 自费。本次公开资料中未找到具体分数权重、最终通过门槛、期末考试安排或 Lab1/Lab2 的统一提交入口、截止日期。不要将“实验不计分”推断为“课程自动通过”。来源、限定范围和新增各讲摘要见 [本次更新说明](update-2026-11-16.md)。

## 版权

课程页面标注为 [Creative Commons BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)。本地文件仅作课程学习归档；转载或再利用请遵守原页面的许可条件。
