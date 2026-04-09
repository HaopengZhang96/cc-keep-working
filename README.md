# keep-working

让 Claude Code 真正"持续工作"指定时长的 Skill —— 通过 `Stop` hook 拦截早停，到点 / 触顶 / 停滞才放行。多会话隔离、停滞检测、可配上限、CLI 工具齐全。

> A Claude Code skill that forces Claude to keep working for a fixed
> wall-clock duration instead of stopping early. Multi-session isolation,
> stagnation detection, configurable caps, CLI helper, full test suite.

[English](#english) | [中文](#中文)

---

## 中文

### 解决什么问题

即使开了所有权限、prompt 里反复强调"连续工作 10 小时"，Claude Code 经常半小时就停下来 — 模型自己觉得"任务告一段落"就触发 Stop。这个 skill 用 `Stop` hook 在它想停时拦下来注入"继续干"，直到真到时间、撞上硬上限、或被检测出真没事干（停滞检测）才放行。

### 特性

- ✅ **指定时长**：小时或分钟（`3 小时` / `30 分钟` / `1.5h` / `90m`）
- ✅ **硬上限防失控**：轮数 + token 双保险，可配可限
- ✅ **多会话隔离**：每个 Claude Code 会话独立 state，互不干扰，可同时跑 N 个
- ✅ **停滞检测**：连续 3 次 Stop 无任何工具调用 → 自动放行（真没事干就别硬撑）
- ✅ **优雅提前结束**：`停止持续工作` → 写 stop-request 标记 → 下次 PreToolUse 自动失活
- ✅ **纯文本 stderr 注入**：与 Claude Code 当前 Stop hook 路径实测兼容（经 [issue #10412](https://github.com/anthropics/claude-code/issues/10412) 的 JSON 路径稳定后可切换）
- ✅ **日志大小封顶**：不会出现 [issue #16047](https://github.com/anthropics/claude-code/issues/16047) 那种 48GB 日志撑爆磁盘的情况
- ✅ **配套 CLI**：`keep-working status / list / stop / clean / log`
- ✅ **幂等 install / uninstall 脚本**：保留你已有的 hooks
- ✅ **完整单元测试**：25 个 test case 覆盖所有边界
- ✅ **路径净化**：session_id 走 hash，防止路径穿越
- ✅ **state 文件 chmod 600**：task 描述可能含敏感信息

### 工作原理

```
用户: 请持续工作 3 小时，重构 auth 模块
       │
       ▼
Skill 写 ~/.claude/keep-working-pending.json
       │
       ▼
Claude 调用第一个工具 ──► PreToolUse hook (bind)
                              │
                              ▼
                  原子 rename 成
                  ~/.claude/keep-working/<sid_hash>.json
                  (绑定到本会话)
                              │
                              ▼
Claude 工作 ──► 想停止 ──► Stop hook
                              │
                ┌─────────────┼──────────────┬────────────┐
                ▼             ▼              ▼            ▼
            未到点 &     到点 / 超轮      停滞 3 次     stop_hook_active
            未停滞       / 超 token       无新工具      (递归保护)
                │           │              │              │
            exit 2 +      exit 0         exit 0         exit 0
            JSON stderr   清状态         清状态
            "继续工作"
                │
                └──► Claude Code 把 stderr 当作新一轮 user 消息
                     → 继续工作
```

**多会话并发**：每个会话有独立的 state 文件（按 session_id 的 SHA1 hash 命名），互不干扰。可以同时开 N 个 Claude Code 会话各跑各的持续工作任务。

### 安装

**方式一：脚本安装（推荐）**

```bash
git clone https://github.com/<you>/keep-working.git
cd keep-working
bash keep-working/install.sh
```

`install.sh` 会：
1. 把 skill 文件拷到 `~/.claude/skills/keep-working/`
2. 把 hook 脚本拷到 `~/.claude/hooks/keep-working.py`
3. 幂等合并 PreToolUse + Stop hook 到 `~/.claude/settings.json`（保留你已有的 hooks）
4. 拷贝 CLI 助手到 skill 目录

二次运行安全。卸载用 `uninstall.sh`。

**方式二：手动安装**

```bash
mkdir -p ~/.claude/skills/keep-working ~/.claude/hooks
cp -r keep-working/keep-working/* ~/.claude/skills/keep-working/
cp keep-working/keep-working/hooks/keep-working.py ~/.claude/hooks/keep-working.py
chmod +x ~/.claude/hooks/keep-working.py
```

然后合并到 `~/.claude/settings.json`（**保留你已有的 hooks**）：

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/keep-working.py bind" }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          { "type": "command", "command": "python3 ~/.claude/hooks/keep-working.py stop" }
        ]
      }
    ]
  }
}
```

> ⚠️ **不要装成 plugin**。Claude Code [issue #10412](https://github.com/anthropics/claude-code/issues/10412) 表明 plugin 形式的 Stop hook exit-code-2 可能被无视。必须装到 `~/.claude/hooks/` 这个路径。

### 使用

**触发短语**（必须包含时长）：

| 中文 | English |
|---|---|
| `请持续工作 3 小时，重构 auth` | `keep working for 3 hours on the refactor` |
| `连续工作 90 分钟，做 X` | `work continuously for 90 min on X` |
| `不要停，做 1.5h` | `nonstop, 1.5h` |
| `连续 5 小时，上限 300 轮 5M tokens` | `keep working 5h, max 300 turns 5M tokens` |

**提前结束**：

- `停止持续工作` / `结束持续工作` / `取消持续工作`
- `stop keep working` / `cancel keep working`

### 性能

Hook 路径每次 Stop/PreToolUse 都会执行 — 必须快。实测：

| 场景 | 耗时 |
|---|---|
| 空 transcript Stop | ~10ms |
| 14MB transcript 首次 Stop（cap 5MB 扫） | ~33ms |
| 14MB transcript 后续 Stop（增量扫 ~200B） | ~19ms |
| PreToolUse bind（无 pending） | ~10ms |
| PreToolUse bind（有 pending，需原子 rename） | ~15ms |
| 200 次顺序 Stop 总耗时 | ~3.8s（avg 19ms） |
| 20 并发 bind 抢占 pending | 一个胜出，其余 no-op |

增量扫描：hook 把上次扫到的字节偏移存进 state，下次从那里读起。首次扫描受 `KEEP_WORKING_SCAN_MAX_BYTES`（默认 5MB）约束，之后只读增量。这样即使会话 transcript 涨到上百 MB，单次 Stop hook 耗时也稳定在 20ms 数量级（含 Python 解释器启动开销）。

自己量一下：`bash keep-working/examples/benchmark.sh`。

### CLI 工具

`bin/keep-working` 是配套命令行（不依赖 Claude Code 运行）：

```bash
keep-working status              # 详细查看所有活跃 session
keep-working list                # 一行一 session 简表
keep-working stop                # 写 stop-request，下次工具调用时失活
keep-working extend 30           # 把所有活跃 session deadline 延长 30 分钟
keep-working extend 30 -s abc    # 只延长 session_id 含 "abc" 的
keep-working extend -15          # 反过来缩短 15 分钟（负数）
keep-working config              # 展示当前 env var 配置
keep-working doctor              # 自检安装健康度
keep-working clean               # 强制清掉所有 state 文件
keep-working log -n 100          # 看 hook 日志
keep-working version
```

可以放进 `$PATH`：`ln -s ~/.claude/skills/keep-working/bin/keep-working ~/bin/keep-working`

### 上限（防失控）

| 项目 | 默认 | 硬顶 | env 覆盖 |
|---|---|---|---|
| 时长 | 用户必填 | 24 小时 | — |
| max_turns | 200 | 1000 | — |
| max_tokens | 2,000,000 | 20,000,000 | — |
| nudge_count（防御深度） | 500 | 5000 | `KEEP_WORKING_NUDGE_CAP` |
| 停滞放行阈值 | 3 次空 Stop | — | `KEEP_WORKING_STAGNATION_CAP` |
| 孤儿文件 TTL | 24h | — | `KEEP_WORKING_ORPHAN_TTL_SEC` |
| Pending 文件 TTL | 10 min | — | `KEEP_WORKING_PENDING_TTL_SEC` |
| 日志大小 | 1MB | — | `KEEP_WORKING_LOG_MAX_BYTES` |
| 增量扫窗口 | 5MB | — | `KEEP_WORKING_SCAN_MAX_BYTES` |
| Claude 配置目录 | `~/.claude` | — | `CLAUDE_HOME` |
| Deadline 硬地平线 | 25 小时 | — | `KEEP_WORKING_MAX_HORIZON_SEC` |

任一上限触发都会立即放行 + 清状态。

### Token 计数说明

**取每条消息的 max，不是 sum**。每次 API 调用的 `input_tokens` 字段已经包含完整上下文，**累加会严重重复计数**（v0.1 的 bug）。我们改为取所有 message 中 `input + output + cache_creation + cache_read` 的最大值，作为"当前上下文占用"的近似指标。

不准确，但比累加准确。别拿来做计费。

### 停滞检测

如果 Claude 连续 3 次 Stop 之间一个工具调用也没有，说明它真没事可做了 → 自动放行。这避免了"已经做完了但 hook 还在硬塞继续"的死循环。SKILL.md 也告诉 Claude：在 keep-working 模式下不要问问题（自治），如果真做完了就停手别动。

### 调试 / 故障排查

```bash
# 看 hook 日志
keep-working log -n 100

# 看当前所有 session
keep-working status

# Hook 完全无声音？检查 Stop hook 安装：
python3 -c "import json; print(json.load(open('$HOME/.claude/settings.json'))['hooks'])"

# Stop hook 没生效，可能是日志撑爆磁盘的老 bug：
ls -lh ~/.claude/hooks.log 2>/dev/null  # 如果是 GB 级别就 rm

# 启动后想从头跑：
keep-working clean
```

### 注意事项

1. **进程要活着**：这个 skill 只在 Claude Code 进程运行时有效。关掉终端 = 计时器作废，没有后台守护。
2. **不要装成 plugin**：见上面的 issue #10412。必须 raw skill 形式装到 `~/.claude/skills/`。
3. **不要在 keep-working 模式下问问题**：SKILL.md 已经告诉 Claude 不要问，但如果它还是问了，hook 会强行让它继续 — 你的问题会被忽略。要中断就用 `停止持续工作`。
4. **token 是近似值**：见上面 Token 计数说明。
5. **同时跑多个**：可以的。每个会话独立。`keep-working list` 看全部。

### 更多文档

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — 设计决策、踩过的坑、现在为什么是这样
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — hook 不工作时的排查步骤
- [`CHANGELOG.md`](CHANGELOG.md) — 版本历史
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — 贡献指南

### 致谢

- 设计灵感参考 [`andylizf/nonstop`](https://github.com/andylizf/nonstop) 的 nudge_count + session-scoped 模式
- Hook 协议参考 [Claude Code Hooks Guide](https://code.claude.com/docs/en/hooks-guide)

### 文件结构

```
keep-working/
├── README.md                    # 你正在看的这个
├── CHANGELOG.md
├── keep-working/                # ← 这一层放进 ~/.claude/skills/
│   ├── SKILL.md                 # Skill 元信息 + 给 Claude 的指令
│   ├── README.md                # Skill 自身的说明（可选）
│   ├── install.sh               # 幂等安装脚本
│   ├── uninstall.sh
│   ├── hooks/
│   │   └── keep-working.py      # Stop + PreToolUse hook
│   ├── bin/
│   │   └── keep-working         # CLI 助手
│   └── tests/
│       └── test_hook.py         # 25 个 unittest
```

### License

MIT

---

## English

### What it solves

Even with every permission granted and a prompt screaming "WORK FOR 10
HOURS", Claude Code routinely calls it quits after 30 minutes — the model
decides the task is "done enough" and stops. This skill installs a `Stop`
hook that catches that moment, injects a "keep going" message, and only
lets the stop through when the wall-clock deadline, a hard cap, or the
stagnation detector says it's actually done.

### Features

- ✅ Specify hours OR minutes (`3 hours` / `30 min` / `1.5h` / `90m`)
- ✅ Hard caps on turns and tokens, configurable, with hard ceilings
- ✅ Multi-session isolation — run N concurrently, no interference
- ✅ Stagnation detection — releases after 3 empty stops in a row
- ✅ Clean early stop via `~/.claude/keep-working-stop-request` flag
- ✅ Plain-text stderr injection (empirically compatible with current Claude Code; #10412 JSON path to be enabled later)
- ✅ Log size capped (1MB) — won't repeat the 48GB disk-fill bug (#16047)
- ✅ CLI helper: `keep-working status / list / stop / clean / log`
- ✅ Idempotent install / uninstall scripts that preserve existing hooks
- ✅ 25-test unittest suite with sandboxed CLAUDE_HOME
- ✅ session_id hashed → safe against path traversal
- ✅ State files chmod 600 (task descriptions may be sensitive)

### How it works

```
You: keep working for 3 hours on the auth refactor
       │
       ▼
Skill writes ~/.claude/keep-working-pending.json
       │
       ▼
Claude makes its first tool call ──► PreToolUse hook (bind)
                                            │
                                            ▼
                              atomic rename to
                              ~/.claude/keep-working/<sid_hash>.json
                                            │
                                            ▼
Claude works ──► tries to stop ──► Stop hook
                                       │
                       ┌───────────────┼─────────────┬──────────────┐
                       ▼               ▼             ▼              ▼
                   before              cap hit       3 empty       stop_hook_
                   deadline &          (time/turn/   stops in a    active
                   under caps          token)        row           (recursion)
                       │                  │            │              │
                   exit 2 +            exit 0       exit 0         exit 0
                   JSON stderr         clear        clear
                   "keep going"
                       │
                       └──► Claude Code feeds stderr back as a user turn
                            → work resumes
```

**Concurrent sessions**: each session has its own state file under
`~/.claude/keep-working/`, keyed by SHA1 hash of `session_id`. Run N
keep-working sessions in parallel without interference.

### Install

**Recommended:**

```bash
git clone https://github.com/<you>/keep-working.git
cd keep-working
bash keep-working/install.sh
```

The installer:
1. Copies the skill to `~/.claude/skills/keep-working/`
2. Copies the hook to `~/.claude/hooks/keep-working.py`
3. Idempotently merges PreToolUse + Stop hooks into `~/.claude/settings.json` (preserves your existing hooks)
4. Drops the CLI helper into the skill directory

Re-running is safe. Use `uninstall.sh` to remove cleanly.

**Manual install:** see the Chinese section above for the JSON snippet.

> ⚠️ **Do not install as a plugin.** Claude Code [issue #10412](https://github.com/anthropics/claude-code/issues/10412) shows that plugin-installed Stop hooks have their exit code 2 ignored. Always install to `~/.claude/hooks/` directly.

### Usage

**Trigger phrases** (must include a duration):

- `keep working for 3 hours on the refactor`
- `work continuously for 90 min on X`
- `nonstop, 1.5h`
- `keep working 5 hours, max 300 turns 5M tokens`
- 中文同样支持，见上面"使用"小节

**Stop early**:

- `stop keep working` / `cancel keep working` / `end keep-working`

### CLI

```bash
keep-working status   # detailed view of all active sessions
keep-working list     # one-line-per-session
keep-working stop     # write stop-request flag
keep-working clean    # nuke all state files
keep-working log      # tail hook log (default last 50 lines)
keep-working version
```

### Caps

| What | Default | Hard ceiling | Env override |
|---|---|---|---|
| `hours` | required | 24 | — |
| `max_turns` | 200 | 1000 | — |
| `max_tokens` | 2,000,000 | 20,000,000 | — |
| nudge_count (defense in depth) | 500 | 5000 | `KEEP_WORKING_NUDGE_CAP` |
| stagnation threshold | 3 empty stops | — | `KEEP_WORKING_STAGNATION_CAP` |
| orphan TTL | 24h | — | `KEEP_WORKING_ORPHAN_TTL_SEC` |
| log size | 1MB | — | `KEEP_WORKING_LOG_MAX_BYTES` |

### Token counting

We take **MAX over messages**, not SUM. Each Anthropic API call's
`input_tokens` already includes the entire prior context, so summing them
double-counts massively (this was a v0.1 bug). Now we use the largest
single message's `input + output + cache_creation + cache_read` as the
"current context size" approximation.

It's approximate. Don't bill against it.

### Stagnation detection

If Claude calls Stop 3 times in a row without making any tool calls in
between, the hook concludes there's genuinely nothing left to do and
releases the session. This prevents the "task is finished but the hook
keeps stuffing 'keep going' down its throat" failure mode. The SKILL.md
also tells Claude: in keep-working mode, don't ask questions — pick a
default and proceed. If you're truly done, just stop and the stagnation
detector will release you.

### Caveats

1. Only works while the Claude Code process is alive — no background daemon.
2. Don't install as a plugin (issue #10412).
3. In keep-working mode Claude is told NOT to ask questions; if it does, the hook will override and force continuation. To break out, use `stop keep working`.
4. Token counts are approximate. Don't use for billing.

### More docs

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — design decisions, failed approaches, why the code is the way it is
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — diagnosing hook failures
- [`CHANGELOG.md`](CHANGELOG.md)
- [`CONTRIBUTING.md`](CONTRIBUTING.md)

### Credits

- Design inspiration from [`andylizf/nonstop`](https://github.com/andylizf/nonstop) (nudge_count + session-scoped pattern)
- Hook protocol from [Claude Code Hooks Guide](https://code.claude.com/docs/en/hooks-guide)

### License

MIT
