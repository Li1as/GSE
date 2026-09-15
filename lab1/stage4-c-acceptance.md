# 阶段 4C 验收记录

验收日期：2026-09-16。

## 实现结果

- `mail_web.py` 在原 Bearer token、同源检查和 `no-store` 响应边界内新增
  ehall 任务列表、创建、详情、资料回答、任务决定、字段编辑、重新准备、
  附件上传和临时登录二维码接口。对外任务不返回 URL query 或 fragment。
- `web/index.html` 和 `web/app.js` 新增对应的手机交互。课程决定使用页面实际选项，
  字段编辑使用乐观版本号；界面明示 4C 没有退课提交按钮。
- `EhallAttachmentStore` 将附件按 SHA-256 存入私有目录，文件名不参与路径解析；
  编辑和后续准备前重新校验文件长度、内容哈希、普通文件类型和非符号链接。
- `ehall_worker.py` 使用持久 `ehall_jobs` 队列，只消费用户已创建或明确重新
  准备的任务，不扫描站点。登录过期、页面变化和回读不一致都会持久暂停；
  并发编辑导致的旧 worker 结果被丢弃，不覆盖新版本。
- 登录成功后显式保存权限 600 的 Playwright storage state，包括原本会在浏览器
  进程结束后丢失的 session cookie。二维码仍在成功、超时或失败后删除。

## 退课适配器的 4C 安全边界

真实课表同一课程会因表格冻结列产生重复 DOM。适配器通过
`a#kblbtk.j-row-edit` 识别“退课”入口，只读取 `data-jxbid`、
`data-jxbmc` 和 `data-action`，按教学班 ID 去重。用户选课后，4C 的
`fill/read_back` 只唯一定位这些属性并验证前后一致；不调用 `click()`、
不派发事件、不调用站点 JavaScript。适配器仍不实现 `submit`。

## 自动验收

合成测试覆盖：手机 API 认证与二维码读取、附件上传与篡改拒绝、创建后入队、
课程选择、字段编辑、旧版本冲突、假页面重复控件去重、试填回读、
登录过期与页面变化暂停，以及适配器不存在 `submit` 能力。
执行 `PYTHONPATH=lab1 python3 -m unittest discover -s lab1 -p 'test_*.py'`，
Lab1 全量 196 项测试全部通过。

## 真实页面验收

用户扫码后，worker 在真实“我的课表”页面成功识别 3 个去重后的当前
可退课选项，父任务按设计停在 `waiting_input`，没有从描述或页面中任选课程。
关闭首个浏览器进程后，第二个新进程成功恢复私有会话并再次识别
`my_timetable`，未生成新二维码。整个过程未点击任何退课入口。

用户随后明确指定两门课。站点的显示名称含额外排版或标记，系统先拒绝了
不成立的完全字符串匹配，经修正后从同一行的课程名称列取值，并对两个用户名称
分别得到唯一、且教学班 ID 不同的匹配。按“每个高后果外部操作一个父任务”
拆分后，两个任务均在真实页面完成控件定位和回读，状态均为
`preview_ready`，两条回读记录均为 `submitted=false`。

## 服务部署验收

Playwright 1.48.0 与对应 Chromium 已安装到被 Git 忽略的
`data/ehall/python-packages` 和 `data/ehall/browsers`，服务不再依赖 `/tmp`
中的临时安装。新增 `gse-ehall-worker.service` 并纳入
`restart_and_verify.sh`。重启后 `gse-mail-web`、`gse-mail-scheduler` 与
`gse-ehall-worker` 均为 `active`；本机受保护接口返回 2 个 ehall
任务，两者均保持 `preview_ready` 与 `submitted=false`。二维码文件已删除，
会话状态文件权限为 600。
