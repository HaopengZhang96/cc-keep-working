# cc-keep-working

让 Claude Code 真正"持续工作"指定时长，不再半小时就停。

A Claude Code skill that forces Claude to keep working for a specified duration instead of stopping early.

**[中文文档](README_CN.md)** | **[English Docs](README_EN.md)**

---

## Quick Start

```bash
git clone https://github.com/HaopengZhang96/cc-keep-working.git
cd cc-keep-working
bash keep-working/install.sh
```

Then in a **new** Claude Code session:

```
请持续工作 3 小时，重构 auth 模块
keep working for 3 hours on the auth refactor
```

## How it works

```
User: keep working for 3 hours
       │
       ▼
Skill writes pending state file
       │
       ▼
First tool call ──► PreToolUse hook (bind)
                         │
                         ▼
              Atomic rename → per-session state file
                         │
                         ▼
Claude works ──► tries to stop ──► Stop hook
                                       │
                   ┌───────────────────┼──────────────────┐
                   ▼                   ▼                  ▼
              before deadline     cap / stagnation    stop_hook_active
              & under caps        hit                 (recursion guard)
                   │                   │                  │
              exit 2 + stderr      exit 0              exit 0
              "keep going"         clear state
                   │
                   └──► Claude Code feeds back → work resumes
```

## Features

- ✅ Hours or minutes (`3h` / `30 分钟` / `90m`)
- ✅ Multi-session isolation (SHA1-hashed state files)
- ✅ Stagnation detection (3 consecutive empty stops → auto-release)
- ✅ Incremental delta transcript scan (~20ms per stop)
- ✅ Atomic bind via `os.rename` (no race conditions)
- ✅ Bilingual continuation messages (Chinese / English auto-detect)
- ✅ CLI: `status --json` / `list` / `stop` / `extend` / `doctor` / `config` / `clean` / `log`
- ✅ 61 unit tests, CI on ubuntu + macOS × Python 3.9–3.12
- ✅ Defense in depth: deadline horizon clamp, nudge cap, chmod 600, log size cap

## Docs

| Doc | Content |
|---|---|
| [README_CN.md](README_CN.md) | 完整中文文档 |
| [README_EN.md](README_EN.md) | Full English docs |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Design decisions & trade-offs |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Debugging hook failures |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution guidelines |

## License

MIT
