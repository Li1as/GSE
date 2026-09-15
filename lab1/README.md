# Lab1：会成长的个人助手开发指导

第二阶段 2A—2E 的约定最小流程已完成，用户已确认真实发送成功；107 项自动测试及真实发送的 13 项归档检查通过。逐项完成情况与保留边界见 [2E 验收及第二阶段总结](stage2-e-acceptance.md)。可用 `python3 lab1/mail_delivery_audit.py` 检查并导出最近一次已接收邮件的冻结内容与回执。

第一阶段 A（一次性资料整理）、B（API 检索）、C（文本补充）、D（更新与单机中断恢复）均已实现并验收。详细设计见 [stage1-design.md](stage1-design.md)，阶段完整性评估见 [小步 D 验收](stage1-d-acceptance.md)。早期验收记录 [A](stage1-a-acceptance.md)、[B](stage1-b-acceptance.md)、[C](stage1-c-acceptance.md) 保留当时实现状态。

第二阶段详细设计见 [stage2-design.md](stage2-design.md)。2A 已通过真实 IMAP 登录、邮件列表、正文读取、快照、未读标志保持，以及跨收件箱和已发送目录的回复关联与附件校验。详见 [2A 验收记录](stage2-a-acceptance.md)。

第四阶段详细设计见 [stage4-design.md](stage4-design.md)。ehall 与阶段三邮件监控采用不同入口：系统不持续读取、发现或分类 ehall 事务；只有用户提交具体任务 URL 后才建立任务，并可同时提供仅作用于本任务的简要描述和可选附件。其余信息由 Agent 读取页面后按实际字段追问。

阶段 4A 提供 URL 校验及只读浏览器探测，不填写或提交事务表单。复制 `ehall.example.json` 为被 Git 忽略的 `ehall.local.json`；安装 `requirements-ehall.txt` 及对应 Chromium 后运行：

```bash
python3 lab1/ehall_browser.py check-url '<用户提供的 ehall URL>'
python3 lab1/ehall_browser.py probe '<用户提供的 ehall URL>'
# 可覆盖二维码等待时间，范围为 30—600 秒
python3 lab1/ehall_browser.py probe '<用户提供的 ehall URL>' --qr-wait 300
python3 -m unittest discover -s lab1 -p 'test_ehall_browser.py' -v
```

需要登录时，程序不读取或填写账号密码，而是把短期二维码保存到 `data/ehall/login/qr.png` 并等待人工扫码；成功或超时后立即删除二维码。4C 已可通过 Bearer token 保护的手机接口展示同一临时文件，登录后会将会话状态以 600 权限保存在被 Git 忽略的私有目录。探测结果保存到 `data/ehall/probes/<编号>/`，其中可能含课表、页面文字和截图，始终作为本地私有数据处理。真实登录与只读访问见 [4A 验收记录](stage4-a-acceptance.md)；持久父任务、退课字段模型和资料补充隔离见 [4B 验收记录](stage4-b-acceptance.md)；手机编辑、持久 worker 与非提交回读见 [4C 验收记录](stage4-c-acceptance.md)；冻结预览、独立确认和一次性退课见 [4D 验收记录](stage4-d-acceptance.md)；归档恢复与完整流程见 [4E 验收记录](stage4-e-acceptance.md)。

4C 的 worker 只消费用户创建或明确重新准备后入队的任务：

```bash
python3 lab1/ehall_worker.py once
python3 lab1/ehall_worker.py run --interval 2
```

作为长期服务运行前，在被 Git 忽略的私有目录安装固定依赖与 Chromium（不需要 root）：

```bash
python3 -m pip install --target lab1/data/ehall/python-packages -r lab1/requirements-ehall.txt
PYTHONPATH=lab1/data/ehall/python-packages PLAYWRIGHT_BROWSERS_PATH=lab1/data/ehall/browsers \
  python3 -m playwright install chromium
./lab1/restart_and_verify.sh
```

`gse-ehall-worker.service` 与邮件调度器分离；它只消费持久队列，不定时扫描 ehall。

手机端可创建 ehall 任务、查看扫码二维码、回答资料追问、选择具体课程、编辑当前字段及管理可选附件。4D 会展示冻结字段、附件、后果与指纹，要求每个退课任务独立勾选确认。确认后直接入队，worker 重新回读一致后只执行一次站点退课提交；点击后结果不明时禁止自动重试。

4E 已完成 ehall 归档/恢复、审计保留和完整流程验收。“嵌入式系统01班”的真实流程已取得站点成功回执并归档，详见 [4E 验收记录](stage4-e-acceptance.md)。

## 使用小步 3E：持续自动处理

功能更新后，可从仓库根目录运行以下脚本完成离线检查、服务模板同步、重启和本机接口验证：

```bash
./lab1/restart_and_verify.sh
```

默认会运行完整离线测试；仅需快速重启与健康检查时可使用 `--skip-tests`。脚本不会打印网页访问口令、邮件正文或凭据，并会验证未授权接口返回 401、两个历史列表最多返回 10 条。它管理的是当前用户的两个 systemd 服务，不需要 `sudo`。

统一调度器按顺序执行增量收信、分类和任务分派。它不会自动确认或发送邮件：

```bash
# 执行一轮
python3 lab1/mail_scheduler.py once
# 每 60 秒持续运行，Ctrl+C 安全停止
python3 lab1/mail_scheduler.py run --interval 60
# 查看、暂停或恢复持久调度状态
python3 lab1/mail_scheduler.py status
python3 lab1/mail_scheduler.py pause
python3 lab1/mail_scheduler.py resume
```

手机网页现在显示自动化状态、积压和分类队列，并可暂停、恢复、立即扫描及纠正单封邮件分类。长期运行服务模板位于 `systemd/`，当前机器已安装并启用；迁移到其他机器时需核对实际 Python 路径、WSL 的 systemd 状态和局域网转发。

分类为“无需回复”的邮件（包括人工纠正为无需回复的邮件）可点击“标记已处理并隐藏”。该操作不修改分类或邮箱已读状态；邮件保留在“已处理邮件”区域，可随时恢复。若分类后来发生变化，旧隐藏标记自动失效。

SMTP 已接收的回复任务可单独归档。归档会从默认任务列表和自动邮件队列隐藏该任务，但完整草稿、确认与发送审计记录仍保留；恢复后才能继续操作。无需回复的“已处理”和成功回复的“已归档”语义分开，两类历史区域都只显示最近加入的 10 条，避免长期运行后页面无限增长。

详见 [3E 验收及前三阶段总结](stage3-e-acceptance.md)。

## 使用小步 3D：核对新的相关往来

3D 会把已扫描且回复头可验证的新往来关联到未完成任务。任务详情页会保留现有草稿，并要求选择“纳入新往来并重新准备”或“标记为与本次回复无关”。完成选择前不能编辑、确认或发送，旧发送预览自动失效。

也可使用 CLI：

```bash
python3 lab1/mail_pipeline.py review-update --task-id <任务ID> \
  --source-id <新往来源ID> --action include
python3 lab1/mail_pipeline.py review-update --task-id <任务ID> \
  --source-id <新往来源ID> --action ignore
```

`include` 以新邮件为回复对象重新准备，并把此前的用户编辑作为参考；`ignore` 原样保留草稿内容但生成需要重新确认的新版本。两者都不会自动发送。详见 [3D 验收记录](stage3-d-acceptance.md)。

## 使用小步 3C：统一邮件任务入口

3C 将 3B 的分类结果接入阶段二邮件任务。自动和手动入口按完整 IMAP 身份复用同一任务；分类改为 `no_reply` 或 `user_review` 时暂停已有自动任务并阻止发送预览，改回 `reply_required` 后继续同一任务。

```bash
# 对已分类邮件执行一轮任务编排
python3 lab1/mail_pipeline.py once
# 持续执行采集、分类和任务编排
python3 lab1/mail_pipeline.py run --interval 60
```

无历史邮件默认采用简洁、礼貌、偏正式且跟随来信主要语言的风格，不自动推断关系或添加签名；混合语言时会生成一次性风格问题。一次草稿修改不会自动形成长期偏好。实现范围、首次验收失败和修复见 [3C 验收记录](stage3-c-acceptance.md)。

## 使用小步 3B：邮件分类与人工纠正

3B 对 3A 已采集为 ready 的邮件分类，不读取个人资料库、不创建回复任务、不发送邮件。分类为 `reply_required`、`no_reply` 或 `user_review`，结果绑定邮件快照哈希并持久化。

```bash
# 先执行一轮增量采集，再分类到期的 ready 邮件
python3 lab1/mail_classifier.py once
# 持续执行采集与分类，默认每 60 秒一轮
python3 lab1/mail_classifier.py run --interval 60
# 查看分类记录；无需模型配置或网络
python3 lab1/mail_classifier.py list
```

API 暂时错误按有限预算重试，失败不会被记为无需回复。`needs_review` 和 `failed` 不会自动重跑；核对后可指定记录重试：

```bash
python3 lab1/mail_classifier.py retry --validity <UIDVALIDITY> --uid <UID>
```

人工纠正会保留模型原判和纠正历史，不调用模型，也不自动推广为长期规则：

```bash
python3 lab1/mail_classifier.py correct --validity <UIDVALIDITY> --uid <UID> \
  --category no_reply --reason '本次确认不需要再次回复'

python3 lab1/mail_classifier.py correct --validity <UIDVALIDITY> --uid <UID> \
  --category reply_required --reason '对方明确要求确认' \
  --suggested-goal '准备一封确认邮件'

python3 lab1/mail_classifier.py correct --validity <UIDVALIDITY> --uid <UID> \
  --category user_review --reason '意图不明确' \
  --decision-question '你是否希望回复这封邮件？'
```

CLI 当前是人工纠正入口；手机端分类列表和纠正操作属于 3E。分类后的自动邮件任务创建和统一去重已在 3C 实现。真实 API 验收可运行 `python3 lab1/live_classifier_acceptance.py`，它复制最近的 ready 快照到隔离目录，不修改生产分类状态。

详见 [第三阶段设计](stage3-design.md)和[3B 验收记录](stage3-b-acceptance.md)。

## 使用小步 3A：持续增量收信

第三阶段设计见 [stage3-design.md](stage3-design.md)。3A 只采集并保存邮件，不分类、不调用模型、不创建回复任务、不发送邮件；后续分类和网页队列属于 3B—3E。

```bash
# 执行一轮；首次默认以当前 UIDNEXT-1 建立基线，不补录旧邮件
python3 lab1/mail_monitor.py once
# 持续采集，默认每 60 秒一轮；Ctrl+C 停止，重启沿用游标
python3 lab1/mail_monitor.py run --interval 60 --batch 50
# 本地状态、按邮件查看采集结果（不连接邮箱）
python3 lab1/mail_monitor.py status
python3 lab1/mail_monitor.py list
```

状态库位于 `data/monitor/inbox.sqlite`，快照位于 `data/monitor/mail/<账号端点标识>/<快照编号>/`。3C 已将分类结果接入第二阶段邮件任务；手机端分类列表仍待 3E。ready 仅表示采集完成，尚未分类。needs_review 表示超限、解析失败或重试耗尽，需人工查看；retry 等待自动退避。旧代记录保留，总计与当前代计数分别显示。

`--span` 控制每轮搜索的 UID 数值区间大小（默认 1000），`--batch` 控制正文采集数（默认 50）。积压逐轮继续，不截取最近 N 封。邮件到达后即使已在其他客户端标为已读，仍按 UID 采集；程序使用只读 IMAP 和 BODY.PEEK，不改变邮箱标志。

```bash
# 仅首次初始化时选择从现存邮件开始；已有游标不会被该参数重置
python3 lab1/mail_monitor.py once --include-existing
# 已初始化的队列显式补录历史区间，代标识应使用 status 中核对后的值
python3 lab1/mail_monitor.py once --accept-validity <当前UIDVALIDITY> --backfill <起始UID> <结束UID>
# 修正本地凭据后，显式恢复已暂停的连接
python3 lab1/mail_monitor.py once --resume
# UIDVALIDITY 改变时，核对 observed_validity 后建立新代基线；旧记录保留
python3 lab1/mail_monitor.py once --accept-validity <核对后的UIDVALIDITY>
# 重置某条失败记录的采集预算，然后执行 once/run
python3 lab1/mail_monitor.py retry --uid <邮件UID> --accept-validity <当前UIDVALIDITY>
```

默认重建新代时从当前边界开始；如需扫描新代现有邮件，显式同时传 `--include-existing`。不要未经核对接受代变更，以免把旧邮件重新视作新工作。补录区间宽度不得超过 span。单条采集自动尝试最多三次，网络连接按退避恢复，认证失败须显式恢复；错误记录仅保留错误类别。

当前未安装后台服务，也未自动启动常驻监控。终端关闭或机器休眠期间不扫描，恢复后继续处理可见 UID；已被服务器删除且从未采集的正文无法恢复。无人值守服务部署后续单独处理。

```bash
# 3A 隔离模拟测试
python3 -m unittest discover -s lab1 -p 'test_mail_monitor.py' -v
# 真实只读验收，在独立目录验证基线、历史补录、恢复、快照与 FLAGS
python3 lab1/live_monitor_acceptance.py
```

真实验收结果位于 `data/monitor-acceptance/<运行编号>/acceptance.json`。不会设置生产监控起点；具体结果与尚未覆盖的真实场景见 [3A 验收](stage3-a-acceptance.md)。

## 使用小步 2C

2D 已增加发送预览、精确版本确认、抄送/密送、附件和发送记录，详见 [2D 验收记录](stage2-d-acceptance.md)。保存草稿后点击“生成最终发送预览”，核对并勾选，再点击“确认上述版本并发送”会实际发信。没有确认不会发送；结果不明时不能自动重发。

真实浏览器任务的规划与补充问题已修复，详见 [真实任务回归说明](stage2-real-regression.md)。已有任务可使用“按当前决定重新规划”，单项查询错误可单独重试；不必通过反复补充无关字段解除等待。

手机网页已实现，接口与持久化经过自动测试，真机浏览器验收待进行，详见 [2C 验收记录](stage2-c-acceptance.md)。无需安装额外依赖：

```bash
# 本机访问 http://127.0.0.1:8765
python3 lab1/mail_web.py
# 如需可信局域网中的手机访问，改用此命令监听全部网卡
python3 lab1/mail_web.py --host 0.0.0.0 --port 8765
```

启动后在本机打开 `lab1/data/web-token.txt`，将访问口令输入页面。手机使用运行主机可达的局域网 IP 和 8765 端口；WSL 环境须已有相应网络可达性。本次未配置端口转发或系统服务。HTTP 原型仅适用于可信网络，公网部署与 HTTPS 不属于本步。

网页可以查看最近 10 封收件头、只读保存选中邮件，或直接选择现有快照；填写目标后准备回复，可从已有快照中选择最多四封往来。查邮箱使用现有邮件配置，开始任务及提交补充使用现有模型 API。任务页提供进度、缺失资料及范围选择、本次决定、邮件原文、草稿编辑和版本历史。

保存草稿会增加版本；旧页面不能覆盖新版本。遇到冲突时输入保留，可查看最新草稿后合并。已保存修改不自动写入个人信息库，也不调用模型总结。未保存的浏览器输入可能在关闭页面后丢失。列表刷新不覆盖正在编辑的任务，也不调用模型；继续任务使用任务页“重新准备 / 继续”。

后台进程需保持运行；重启后可从任务列表继续已有任务。网页服务运行在 8765 端口，日志位于 `data/mail-web.log`；未安装系统服务。SMTP 验证可执行 `python3 lab1/smtp_probe.py`，此命令只验证认证、不发信。

## 使用小步 2B

`mail_tasks.py` 将一个已保存邮件快照和明确目标变成持久化邮件父任务。模型提取个人事实查询与本次决定，事实通过阶段一子任务查询，资料齐全且决定已回答后生成待审阅草稿。使用现有 API 配置，会将选定邮件、显式提供的往来和必要资料发送到配置的模型服务。

```bash
# 路径替换为 2A 保存的快照目录；--history 可选，最多四封相关往来
python3 lab1/mail_tasks.py create lab1/data/mail/<快照目录> --goal '准备回复邀请，查询要求的资料并询问我是否参加' --history lab1/data/mail/<原邮件快照目录>
python3 lab1/mail_tasks.py show <邮件任务编号>
# request_id 位于父任务 facts 中；scope 必须明确选择
python3 lab1/mail_tasks.py answer <邮件任务编号> <请求编号> --scope task --text '本次任务的事实补充'
python3 lab1/mail_tasks.py answer <邮件任务编号> <请求编号> --scope personal --file <UTF-8文本文件>
# decision_id 位于父任务 decisions 中；决定只影响此邮件任务
python3 lab1/mail_tasks.py decide <邮件任务编号> <决定编号> --text '我决定参加本次活动。'
python3 lab1/mail_tasks.py resume <邮件任务编号>
# 单轮恢复至多四个活动父任务，无后台常驻；可由普通脚本定时调用
python3 lab1/mail_tasks.py worker --limit 4
```

`answer` 复用相关性、冲突检查与 task/personal 隔离，支持 `--confirm-conflict`；`decide` 拒绝无关回复，保留用户决定原文，不归档长期资料。通过邮件入口补充后自动继续父任务；若直接使用阶段一入口补充，则运行邮件 worker 或 resume 同步父任务。

父任务和资料子任务保存在同一 `data/tasks.sqlite` 中的独立表，计划与子任务编号一起提交；单机文件锁协调进程。相同快照、历史和目标的重复 create 复用任务。等待状态无变化时 worker 不调用模型；failed、needs_review、draft_ready 不由 worker 自动重试，使用 resume 显式重新准备。范围决定变化会触发重新规划；也可使用 `replan <任务编号>` 重新评估旧阻塞并保留历史。真实缺少必要附件正文时仍需补齐输入；一般背景限制不会自动当成阻塞。

`waiting_input` 表示缺事实或决定，`needs_review` 表示冲突或其他限制，`failed` 表示执行失败，`draft_ready` 表示草稿可供审阅。show 同时返回草稿 freshness；资料或子任务变化后旧草稿标为 stale，resume 重新查询并生成新版本。历史草稿和当时来源保留。草稿包含收件人建议、正文、版本、来源、用户决定与模型名称；来源编号存在性由程序校验，语义是否充分支持正文仍需人工审阅。

本步没有发送、手机网页或自动附件内容分析。收件人建议取 Reply-To（缺省 From），不自动 reply-all；附件不自动加入回复。普通 thread 仍为单目录扫描，2B 历史由用户显式传入并校验回复头关系，不能宣称会话完整。

```bash
# 虚构邮件、隔离资料库上的真实 API 验收，会使用模型额度
python3 lab1/live_mail_b_acceptance.py
```

详细实现和验收见 [stage2-b-acceptance.md](stage2-b-acceptance.md)。

## 使用小步 2A

邮件凭据单独放在被 Git 忽略的 `lab1/mail.local.json`（权限 600），字段参考 `mail.example.json`。已经创建本地配置；请在邮箱网页完成微信扫码，进入设置中的客户端设置，开启 IMAP 并生成客户端专用密码，用它替换本地配置的 `password`，无需将专用密码发到对话中。

```bash
python3 lab1/mail_reader.py probe
python3 lab1/mail_reader.py --limit 10 list
# 将下方 123 换成 list 中想读取的邮件 UID
python3 lab1/mail_reader.py read 123
python3 lab1/mail_reader.py --limit 30 thread 123
# 真实只读验收：最多 30 封邮件头、一封正文，证据保存在 data/mail
python3 lab1/live_mail_acceptance.py
# 指定带附件的回复 UID，跨已发送目录校验直接原邮件（请替换示例 UID）
python3 lab1/live_mail_thread_acceptance.py 123 --sent-folder 'Sent Messages'
```

`list` 显示最近邮件头；`read` 保存原始 EML 和解析后的 JSON 到 `lab1/data/mail/<身份哈希>/`，并在终端显示正文。`thread` 额外列出当前文件夹有界窗口内的相关邮件头候选，不保证历史完整，也不会自动下载候选正文。可按候选 UID 再 read。当前仅支持 ASCII 文件夹名称，默认 INBOX；不自动探测已发送目录。

跨目录验收脚本接受明确的回复 UID 与已发送目录，验证 In-Reply-To/References、双方地址、原邮件正文与附件字节元信息；证据位于 `data/mail/thread-acceptance.json`。这是独立验收入口，普通 `thread` 命令仍只扫描当前目录。附件保留在原始 EML 中，不执行附件或将其内容交给模型。

所有操作只读，不标已读、不发送邮件、不调用模型。认证失败返回非零且不自动重试。SMTP 配置仅为后续阶段预留，probe 只验证 IMAP。原始邮件本身是 EML/MIME 数据，不属于课程讲义 HTML 存档。

## 运行小步 B

Python 3.8 及以上，仅使用标准库，无需安装第三方依赖。从课程根目录执行：

```bash
python3 lab1/assistant.py check-config
python3 lab1/assistant.py query '数据库这门课我最后拿了多少分？'
python3 lab1/assistant.py query 'CS61A 的成绩是多少？' --json
python3 lab1/assistant.py task <任务编号>
```

真实查询会调用配置的 API，并发送当前任务需要的资料片段。密钥保存在被忽略的 `config.local.json`，示例文件不含凭据。API 基础地址可填写域名、带 `/v1` 的基础路径或完整 `/chat/completions` 端点。所有配置路径中 personal_data_dir 相对于 lab1 解析。

普通输出显示答案与来源编号；`--json` 输出包含完整来源、行号、文件哈希及检索轨迹。任务保存到被忽略的 `data/tasks.sqlite`，可以离线查询已保存结果。更新后的资料不会改写历史证据；历史结果附 freshness=current/stale/unknown，新查询使用当前 Markdown。

`completed` 为有来源的回答，`waiting_input` 为缺资料，`needs_review` 为存在冲突、时效或来源口径限制，`failed` 为执行失败。缺失项生成补充请求，SQLite 是状态依据，`data/requests/<请求编号>.md` 为可阅读、填写的投影。

## 使用小步 C

查询进入 waiting_input 后，终端会显示补充请求编号。以下 `<请求编号>` 替换为实际编号；必须明确选择保存范围：

```bash
python3 lab1/assistant.py request <请求编号>
# 临时回答只影响本次任务
python3 lab1/assistant.py answer <请求编号> --scope task --text '本次活动饮品选择绿茶。'
# 长期资料保留为独立 Markdown，原始简历和成绩单不改写
python3 lab1/assistant.py answer <请求编号> --scope personal --text '填写完整事实及适用时间'
# 支持 UTF-8 Markdown 或纯文本文件；也可编辑请求文件的 reply 标记区域后提交
python3 lab1/assistant.py answer <请求编号> --scope personal --file lab1/data/requests/<请求编号>.md
```

回复经模型判断相关性与冲突，程序校验归档内容必须是回复原文中的片段。无关回复不归档、不解除等待；部分回答可接受，剩余问题生成新的请求并继续等待。同一事实的新旧值冲突时先返回说明，由你核对后使用 `--confirm-conflict` 明确确认，再接受新值。

通过校验的回复会自动继续同一任务编号。长期资料写入 `data/personal/supplement-<请求编号>.md`；临时回答只保存在该任务中，新任务不可访问。后续答案引用用户补充来源，不把补充冒充原成绩单内容。

相同请求、文本、保存范围和确认选项重复提交时不会重复归档或重复调用已经完成的任务；修改其中任一项而原请求已关闭时会报错。接受补充后若续答 API 失败，可以原样重新提交重试，也可由下述 worker 恢复。发布后的补充文件若被用户修改或删除，重复提交不会将其还原。

```bash
# 虚构资料上的真实 API 临时作用域验收，会使用模型额度
python3 lab1/live_c_temporary.py
```

## 使用小步 D

运行环境为 Linux/WSL、Python 3.8+，使用单机文件锁协调多个 CLI/worker 进程。恢复器是普通脚本，没有模型轮值；空闲且资料未变时不会调用模型。

```bash
# 启动时执行一轮恢复；最多重新执行四个任务
python3 lab1/assistant.py worker --once --limit 4
# 持续运行：每轮后等待15秒；可在重启后再次运行此命令
python3 lab1/assistant.py worker --interval 15 --limit 4
# 人工重新执行指定任务，重置自动重试预算，保留任务编号与历史
python3 lab1/assistant.py resume <任务编号>
```

worker 会修复待发布补充文件和缺失的请求投影，恢复中断的 running 任务；资料变化时重新检索等待补充的任务。已完成任务保留历史并标记过时，不自动重复消耗模型额度；需要最新答案时发起 query 或 resume。

暂时性网络/HTTP错误与查询期间资料变化按退避时间重试，最多三次执行尝试，之后需要人工 resume。认证错误和协议错误不默认无限重试。单次网络调用与模型步骤仍受 B 的限制。持锁的活动任务会被跳过，进程退出后由操作系统释放锁。

SQLite 先记录“接受补充＋待归档”，再以临时文件、fsync 和原子替换发布 Markdown，最后标记发布完成；恢复只重做未完成发布。若待发布路径已有不同内容，会报告 ARCHIVE_CONFLICT，保留现场供核对。恢复请求文件时保留 reply 标记内尚未提交的草稿。

本次提供启动命令，不安装系统服务，也未留下常驻后台进程。要持续监控，请保持 worker 进程运行；停止后任务仍保存在数据库，重新启动即可扫描恢复。

```bash
# 隔离虚构资料上的真实 API 恢复、修改和删除验收
python3 lab1/live_d_acceptance.py
```

```bash
# 离线测试，不读取真实密钥、不调用 API，资料使用临时样例
python3 -m unittest discover -s lab1 -p 'test_*.py' -v
# 真实验收：六项查询，会使用当前模型额度
python3 lab1/live_acceptance.py
```

真实验收的汇总位于 `data/live-acceptance.json`，完整查询与证据保存在任务数据库。测试失败返回非零退出码；query 执行失败返回 1，配置／输入错误返回 2。

实验要求见 [Lab1 讲义](../docs/lab1-growing-personal-assistant.md)。实验为建议性、不计分实验，目标是组合个人资料、smail 邮箱、ehall 与手机交互，并让成功流程和用户纠正能够跨会话复用。

## 设计原则

- 先打通一条自己愿意反复使用的小流程，再扩展功能；每个阶段都留下可以实际验收的结果。
- 监控、采集、转发、去重和状态更新由脚本执行。需要理解语义、提取信息、拟稿或归纳经验时才调用模型，不安排 Agent 持续轮值。
- 个人资料管理与任务调度分开。资料模块返回信息或缺失项，任务管理层负责追问、等待、恢复和完成。
- 人工确认由程序执行约束。发送或提交只能使用用户确认的确切版本，不能仅依赖提示词约定。
- 保存任务进度和操作结果，关闭交互窗口或重启程序后能够恢复。
- 成功流程和纠正沉淀为可阅读、可修改的资料、规则、技能或脚本，用 Git 管理实现与规则。
- 以有明确意图、可验证的小变更推进开发。需求变化时同步更新本文与验收依据。

## 总体结构与初步技术方向

初期采用一个后台程序内的多个模块，不必拆分微服务。语言与 Web 框架在实施时选择；API 地址、密钥与模型提示词属于模型调用配置，不代替资料存储和任务状态管理。

| 部分 | 职责 | 初步实现方向 |
| --- | --- | --- |
| 个人资料 | 查询、来源定位、资料更新和归档 | 当前简历＋成绩单一次性转文本并整理；后续仅 Markdown／文本补充；分类、关键词子串匹配和少量别名，不使用向量索引 |
| 任务管理 | 创建任务、记录缺失项、等待补充、恢复执行、管理确认与结果 | 后台业务模块与 SQLite 持久化 |
| smail | 收信、关联往来、判断回复需求、拟稿和发送 | 若账号支持则使用 IMAP/SMTP；需先验证实际支持情况 |
| ehall | 对用户给出的任务 URL 查询、准备材料、填写表单、确认提交和核对回执 | 不持续扫描或判定事务；收到 URL 后由浏览器工具读取页面，并按具体事务适配 |
| 手机交互 | 发起任务、查看进度、补充资料、修改草稿或字段、确认操作 | 响应式网页，初期无需原生 App |
| 模型调用 | 理解需求、提取缺失项、拟稿、提出经验规则 | 统一 API 接口；本地配置已填写，公开示例地址与密钥为空；model 为 gpt-5.6-terra；按任务提供必要资料与规则 |

后台应运行在持续可用的机器上，不依赖某个桌面对话窗口。浏览器插件可以承担入口或操作功能，后台仍需独立保存与恢复任务。

技术参考：[SQLite 事务](https://www.sqlite.org/lang_transaction.html)、[Playwright 登录状态](https://playwright.dev/python/docs/auth)。SQLite 可以保证本地相关状态一起更新，但不能单独保证外部邮件或表单不会重复提交。

## 共享任务与接口约定

这些是后续实现需要表达的信息，不要求立即固定为某种数据库表或 API 格式。

- **任务**：任务编号、类型、目标、关联邮件或事务、当前阶段、错误与结果。ehall 任务以用户提供的 URL 为必要入口，可带仅作用于本任务的简要描述和可选附件；它们不代替资料证据，也不阻止 Agent 按页面实际字段继续提问。
- **资料查询结果**：找到的内容、来源文件与段落位置、缺失字段以及冲突信息。
- **补充请求**：请求编号、关联任务、缺失字段、用途、回答与处理状态。文件可作为原型入口，但不能仅堆积无关联的自然语言留言。
- **待确认内容**：内容版本，以及邮件收件人、正文、附件，或表单字段、材料与操作后果。
- **执行记录**：执行意图、对应确认版本、结果或回执，以及是否需要核对外部状态。

任务的典型过程是：创建 → 检索与准备 → 缺信息时等待补充 → 继续准备 → 等待确认 → 执行 → 核对结果 → 归档。还应能表达失败、取消和结果不明，不能把超时直接视为未执行。

资料模块不直接调度邮件或表单。补充资料后，由任务管理层找到受影响的等待任务，再决定是否恢复。不同任务可以复用同一资料，但本次任务的临时回答不应自动成为长期个人事实。

## 阶段一：最小个人资料检索与任务恢复

### 目标与范围

整理一组最简个人资料，根据自然语言需求找到已有内容；缺失时提出补充请求，用户补充后恢复原任务。

### 大致设计

- Markdown 为资料来源，按目录和少量元信息组织。程序负责查找和读取，模型负责理解查询与组织回答。
- 结果携带文件路径、标题或段落位置，使回答可追溯。
- 资料新增、修改和删除后，更新检索状态；小规模阶段可以采用简单扫描或轻量索引。
- 缺失信息写入关联任务的请求。补充输入仅为 Markdown 或输入框文本，不实现 PDF、DOCX 等附件自动导入。
- 区分长期资料与本次任务信息，记录来源；新旧内容冲突时追问，不静默覆盖。
- 从本阶段开始保存任务状态，避免以后依赖对话历史恢复任务。

### 验收标准

- 对选定的样例查询，能返回正确内容与原文位置；缺失时明确报告，不编造。
- 新增、修改、删除资料后，查询结果相应改变。
- 补充信息后恢复正确任务；多个等待任务不会串用临时回答。
- 程序重启后，等待中的任务与补充请求仍然存在，并可继续处理。

## 阶段二：一封邮件与手机交互的完整流程

### 目标与范围

先手动选定一封邮件，完成“查资料 → 手机补充 → 拟稿 → 编辑确认 → 发送 → 归档”。本阶段不要求持续监控邮箱。

### 大致设计

- 读取邮件及相关往来，提取回复所需信息，通过资料接口查询。
- 建立最小手机网页：任务列表、进度、补充问题、回复草稿编辑和发送确认。
- 草稿生成使用相关历史邮件和必要个人资料，避免每次加载整个邮箱。
- 确认绑定收件人、正文和附件的具体版本。确认后修改内容需要重新确认；执行发送时不再让模型自由改写。
- 保存发送记录和任务结果，完成后归档。

### 验收标准

- 一封缺少必要信息的邮件能完成上述全流程，并能定位使用的个人资料来源。
- 用户编辑后的最终版本与实际发送内容一致；未确认不能发送。
- 手机页面关闭后任务仍保留，重新打开可继续。
- 发送结果不明时进入待核对状态，不直接自动重发。

## 阶段三：持续收信、去重与可靠恢复

### 目标与范围

把阶段二的人工入口扩展为脚本持续采集新邮件，并处理重复检查、重启和异常。

### 大致设计

- 验证 smail 可用访问方式后，由脚本采集邮件，保存稳定标识、会话关联和处理状态。
- 对新邮件按需调用模型，分类为需要回复、无需回复或需要用户判断；保留分类结果以便查询和纠正。
- 重复采集不重复创建同一处理任务。发送动作还需独立记录，不能仅凭“邮件已读”判断是否处理完成。
- 对发送超时等结果不明的情况，先核对外部结果；无法确认时交给用户处理。
- 调度和状态持久化支持程序重启后继续，明确重试与人工介入的边界。

### 验收标准

- 同一邮件被重复检查，不重复创建任务或发送回复。
- 无需回复和需要判断的邮件可在任务记录中找到，用户能纠正漏判。
- 采集、补充、拟稿或待确认期间重启，任务可恢复。
- 发送请求超时的模拟场景不会触发盲目重发。

## 阶段四：接入一种具体 ehall 事务

### 目标与范围

选择一项实际会办理的事务。系统不持续读取 ehall、不枚举可办事项，也不自动判断是否应创建任务；仅当用户提供具体任务 URL 时创建并执行该任务，完成页面查询、材料准备、表单填写、确认和结果归档。用户可随 URL 提供简要口头描述和可选附件，二者都只属于本次任务；其余信息由 Agent 读取页面后按实际字段查询或追问。持续监控资讯后置。

### 大致设计

- 以用户提交的 URL 作为唯一自动处理入口，校验协议、允许域名和页面身份后才读取页面；不设置 ehall 轮询器、发现队列或分类器。
- 复用资料查询、补充请求、手机任务页面与确认机制，针对 URL 对应的具体事务实现表单逻辑。
- 将用户简要描述保存为本次任务输入。页面字段、个人资料与描述不足或冲突时照常追问，不用描述猜测缺失事实或提交意愿。
- 验证浏览器工具、登录过程、会话失效处理、附件上传和字段依赖。
- 手机端既能回答追问，也能直接修改表单字段；邮件模块同样支持追问，不按业务强行限制交互类型。
- 提交前展示最终字段、附件和后果。页面内容或待提交版本变化后重新核对，必要时重新确认。
- 提交后保存回执或状态证据。结果不明先查询事务状态，再决定后续处理。
- 退课、撤销申请等操作不能由模型自行决定。

### 验收标准

- 用户提供 URL 后，至少一种真实事务能够完成信息查询、材料准备和表单填写；未提供 URL 时不会自动发现或创建 ehall 任务。
- 缺少资料时可在手机补充并继续，用户可以修改最终字段。
- 未确认不能提交；执行内容对应确认版本。
- 登录过期或页面不符合预期时明确暂停并显示原因。
- 成功结果可追溯；超时不会直接导致重复提交。

## 阶段五：经验复用与功能扩展

### 目标与范围

验证助手能够从纠正中成长，再扩展资讯监控、更多事务及更丰富的跨工具流程。

### 大致设计

| 反馈内容 | 保存方式 |
| --- | --- |
| 本次任务的临时要求 | 当前任务上下文 |
| 明确的长期个人偏好或事实 | 个人记忆或资料 |
| 可复用的操作步骤和检查方法 | Skill 或脚本 |
| 稳定的项目开发约定 | `AGENTS.md` |

- 模型可提出规则草案；明确纠正按约定保存，涉及泛化的推断先由用户核对。
- 不把所有修改都追加进 `AGENTS.md`；规则应保持适用范围明确，并在相关任务中按需加载。
- 用 Git 管理实现、规则和示例，使调整可审查、可回退；运行状态、密钥、登录凭据与个人数据需要明确存储边界。
- 至少保留一条愿意反复使用的跨工具完整流程。更复杂的示例是：收到办事通知 → 检索资料 → 手机补充 → 准备表单和邮件 → 分别确认提交与发送 → 归档。
- 已完成小流程的验收后，再加入资讯变化监控或更多事务适配器。

### 验收标准

- 例如纠正“报名截止时间不等于活动开始时间”后，在新会话处理另一份通知时能够正确应用。
- 一次性的任务要求不会误变为所有任务的永久规则。
- 规则和成功流程可人工阅读、修改，并有 Git 变更记录。
- 至少一条跨工具流程可以重复使用，并保留必要的来源、确认和结果证据。

## 开发时优先确认的问题

在进入相应阶段时验证，不要求开始前解决所有问题：

1. 第一批个人资料的范围、样例查询和长期归档规则。
2. 后台运行位置、手机访问方式和实际可用的 smail 接口。
3. 第一种 ehall 事务、所需材料、提交后果与成功判据。
4. 模型与浏览器工具的能力边界，以及登录失效和外部结果不明时的处理方式。

每个阶段围绕上述验收场景组织实现与检查，先获取反馈，再决定是否需要更复杂的调度或部署结构；个人资料检索固定采用分类、关键词子串匹配和少量别名，不引入向量索引。

## Git 存档与私有验收数据

仓库保存代码、合成测试、设计和脱敏验收结论；`data/`、本地配置、邮件、数据库和备份不提交。历史验收文档记录当时结果，不表示每次存档都重新调用真实服务。脱敏前的文档与脚本保存在本地 `data/private-archive/pre-git/`。

涉及真实资料的两个验收脚本从本地 JSON 读取预期值：

- `live_acceptance.py`：读取 `data/live-expectations.json`，字段参考 [合成示例](live-expectations.example.json)。
- `live_real_task_regression.py`：默认读取 `data/regression-expectations.json`，可用 `--expectations` 指定路径，字段参考 [合成示例](regression-expectations.example.json)。

当前机器的预期值已迁移保留。新环境需要根据获准使用的资料填写预期值；示例数字均为虚构，不能直接用于验收真实个人资料。运行这两个脚本会调用模型 API。离线单元测试不需要这些私有文件。

Windows 转发脚本要求显式传入 `-ListenAddress` 和 `-WslAddress`，详见 [局域网说明](lan-access.md)。
