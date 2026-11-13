# bili_submit 使用说明

B 站创作中心 **Web 接口**自动投稿工具（纯 HTTP 协议层实现，无 Playwright/Selenium）。
协议对齐 biliup-rs master 分支（preupload → upos 分片上传 → `x/vu/web/add/v3`）。
方案背景见 [README.md](README.md)。

## 1. 安装（已完成的部分）

本机已建好 `.venv`（Python 3.11），依赖：httpx / qrcode / pillow / tzdata。

```bash
cd E:\ai_spider\bilibili
.venv/Scripts/python.exe -m pip install -r requirements.txt   # 换机器时执行
```

所有命令都在项目根目录下以 `.venv/Scripts/python.exe -m bili_submit <命令>` 运行，
下文简写为 `python -m bili_submit`。

## 2. 首次使用

```bash
# 1) 环境与接口自检（不需要登录）
python -m bili_submit doctor

# 2) 登录（二选一）
python -m bili_submit login                      # 终端出二维码，B 站 App 扫码确认
python -m bili_submit login --file cookies.json  # 导入已有 Cookie（dict / biliup 格式 / Netscape）
python -m bili_submit login --qr-png qr.png      # 终端不方便时导出图片再扫

# 3) 核对登录
python -m bili_submit check

# 4) 查询分区 id（可选）
python -m bili_submit categories
```

凭据保存在 `data/credentials.json`（不进版本库）。想让工具拒绝在错误账号上投稿时，
编辑 `data/config.json` 把 `account.expected_uid` 设成你的 UID。

## 3. 投稿

```bash
# 立即排队投稿
python -m bili_submit add \
  --video  D:/videos/demo.mp4 \
  --cover  D:/videos/cover.jpg \
  --title  "视频标题" \
  --desc   "简介" \
  --tags   "标签1,标签2" \
  --tid    21 \
  --copyright 1              # 1 自制 / 2 转载（转载必须 --source）

# 定时：08:00 开始尝试提交，期望 12:00 公开（B 站要求定时发布晚于当前 4h+）
python -m bili_submit add --video a.mp4 --title t \
  --submit-at  "2026-10-26 08:00" \
  --publish-at "2026-10-26 12:00"
```

时间一律按 **Asia/Shanghai** 解释。提交到公开之间还隔着转码与审核，`publish_at`
只是传给 B 站的定时发布时间（`dtime`），不保证那一刻一定公开。

## 4. 执行与调度

```bash
python -m bili_submit run                # 处理一次到期任务
python -m bili_submit run --once 10      # 本轮最多处理 10 个
python -m bili_submit run --loop         # 常驻调度（每 30s 扫描到期任务 + 定期回查状态）
```

每日定时方案（二选一）：
- **Windows 计划任务 / Linux cron**：每天固定时间跑一次 `run`（推荐，简单可控）。
- **常驻进程**：服务器上 `run --loop` 挂着，任务用 `--submit-at` 预约时间。

服务器上用系统服务托管时注意：配置/凭据/数据库都在 `data/` 下，属于本地状态，
迁移机器时整体拷贝。

## 5. 状态回查

```bash
python -m bili_submit list               # 任务列表（状态/计划时间/BV号/最近错误）
python -m bili_submit show --job <id>    # 详情 + 事件流水
python -m bili_submit reconcile          # 手动回查转码/审核/公开状态
python -m bili_submit cancel --job <id>  # 取消未提交的任务
```

## 6. 状态机

```
pending 待处理 → validated 已校验 → uploading 上传中 → uploaded 已上传
  → submitting 提交中 → submitted 已提交 → reviewing 审核中 → published 已公开

异常：pending_auth 待授权（重新 login 后 run 会自动继续）
      retrying 待重试（网络/5xx/限流，指数退避，next_attempt_at 控制下次时间）
      pending_confirm 结果待核对（提交超时/进程中断；reconcile 按标题找回稿件）
      returned 已退回 / manual 待人工处理 / cancelled 已取消
```

- 断点续传：分片进度存在任务行里，进程重启后从上次分片继续；UPOS 会话失效则自动
  重新申请（素材不受影响），最多 `upload.session_restarts` 次。
- 幂等：提交意图先落库再发请求；提交超时不自动重投，进 `pending_confirm` 由
  reconcile 核对。同一账号+同一内容摘要的重复任务在校验阶段直接拒绝（防误放）。
- 每个任务的状态迁移都记录在 `task_events` 表，`show` 可查。

## 7. 配置（data/config.json，首次运行自动生成）

| 键 | 默认 | 说明 |
| --- | --- | --- |
| account.account_id | main | 账号标识（v1 单账号） |
| account.expected_uid | null | 投稿前核对 UID，防投错号 |
| defaults.tid / tags / copyright / no_reprint | 21 / "" / 1 / 0 | add 时的默认值 |
| defaults.line | auto | 上传线路：auto 测速选线，或固定 bda2/bldsa/tx/… |
| upload.max_retries | 5 | 临时错误重试上限 |
| upload.backoff_base_sec / backoff_max_sec | 30 / 3600 | 退避区间 |
| schedule.loop_interval_sec | 30 | `run --loop` 扫描间隔 |
| schedule.reconcile_interval_sec | 600 | 状态回查最小间隔 |
| network.proxy | null | 代理；null 时沿用系统环境代理 |

## 8. 测试与验证状态

- [x] `doctor`：passport 扫码接口 / nav 接口 / preupload 线路接口在线连通性（本机已验证）
- [x] upos 分片协议冒烟（本地假服务器）：分片字节完整性、complete 载荷、断点续传、
      会话失效分类（`tests/smoke_upos.py`，已通过）
- [x] 队列状态机无凭据实测：add → run → 校验 → pending_auth，全程无远端副作用（已通过）
- [ ] **尚未做**：真实登录、真实上传、真实投稿、审核跟踪（对应 README 落地顺序的
      阶段 2/3，需要你在本机扫码后实测）

`python -m bili_submit run` 在投稿期间不会向浏览器自动化靠拢，也不打开任何页面；
全部行为可通过 `data/logs/bili_submit.log` 审计。

## 9. 已知边界

- **2026-10-26 已真实上线**：REA 介绍测试视频投稿成功（BV1PznU6YEM7），全链路
  （扫码登录 → preupload → 分片上传 → 封面 → add/v3 → 状态回查）实测通过。
- 实测教训：`add/v3` 的 `videos[].filename` 必须是 upos 对象名（无桶前缀、无扩展名），
  否则报 21015「视频可能上传过程出现问题」；平台稿件状态以 `state_desc` 为准
  （`-30` 现在表示「审核中」，与旧文档的「审核失败」不同）。
- Web 投稿接口为非公开契约，B 站可能调整；参数以 `reference/` 下的 biliup-rs
  master 源码为基准实现，出现风控/接口变化时先看日志里的 code/message。
- 单账号串行第一版：多个 `run` 进程同时跑安全（租约防重领），但没必要。
- 定时发布 `dtime` 必须晚于当前 4 小时，否则校验阶段直接拒绝。
- 官方开放平台路线（需开发者入驻 + ARC_BASE 权限）未实现；本工具只走创作中心
  Web 接口这一条路线，不会自动切换。
