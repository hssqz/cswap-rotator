# cswap-rotator

[cswap](https://github.com/realiti4/claude-swap) 的自动巡航。
当 active 账号 5h 配额接近上限时，自动切到最闲的备用号。仅支持 macOS。

---

## 安装

一行命令搞定。如果系统里没有 `uv` 和 `cswap`，会自动安装。

```bash
bash install.sh
```

带参数：

```bash
bash install.sh --threshold 95 --margin 25 --grace 10
```

装完**默认即生效**——后台 LaunchAgent 每 10 分钟轮询一次。

> **前置条件**：cswap 至少有 2 个账号。
> 添加账号需要浏览器登录，没法自动化；如果你不到 2 个账号，
> 安装器会打印一份步骤指引。

---

## 轮换机制

每 10 分钟，rotator 醒一次，问自己一个问题：**active 账号是不是快到 5h 上限了？**
是 → 选最闲的备用号切过去。否 → 啥都不做。

决策流程（无状态，每次从零计算）：

```
1. Tier 1 —— 查 active 账号的 5h%：
   • active < 90%               → 跳过 "below threshold"
   • active 还有 <15 分钟自然重置 → 跳过 "active resets soon"
                                  （让窗口自己重置就好）

2. Tier 2 —— 查所有候选账号的 5h%：
   • 没有任何候选严格低于 active → 跳过 + 告警
                                   "ALL accounts at limit"
   • 否则：选 5h% 最低的那个候选
     - 比 active 低 ≥30 个百分点 → switched          (舒适切换)
     - 否则                       → switched_under_pressure + 告警
   • cswap --switch-to <target>
```

切换**只影响下次新开的 Claude Code session**——已经在跑的对话用的是
内存里的旧凭证，**不会被打断**。

---

## 配置

编辑 `~/.config/cswap-rotator/config.json`：

| 字段 | 默认值 | 含义 |
|---|---|---|
| `rotate_threshold_pct` | 90 | active 5h% 超过这个值触发评估 |
| `safety_margin_pct` | 30 | 候选必须比 active 低这么多个百分点才算"舒适切换" |
| `reset_grace_min` | 15 | active 还有这么多分钟内自然重置 → 不切 |
| `dry_run` | false | true = 只记录决策不真切 |
| `adaptive_polling` | true | false = 忽略 hint 文件，固定每 10 分钟查一次 |

改完下次 10 分钟唤醒时自动生效，不用 reload。

---

## 日常操作

```bash
# 看最近的决策
tail -20 ~/Library/Logs/cswap-rotator.log

# 看今天切了几次
grep '"decision":"switched"' ~/Library/Logs/cswap-rotator.log \
  | grep "$(date +%Y-%m-%d)" | wc -l

# 立刻跑一次（不等下次 10 分钟唤醒）
launchctl kickstart -k gui/$(id -u)/com.cswap-rotator

# 暂停 / 恢复
launchctl unload ~/Library/LaunchAgents/com.cswap-rotator.plist
launchctl load   ~/Library/LaunchAgents/com.cswap-rotator.plist
```

---

## 卸载

```bash
bash install.sh --uninstall   # 删 plist + 脚本，保留 config 和 log
bash install.sh --purge       # 连 config 和 log 也清掉
```

`cswap` 和 `uv` 不会被卸载——这个脚本不管它们。

---

## 文件落地位置

```
~/.cswap-rotator/                       # 脚本（隐藏目录，类似 ~/.pyenv ~/.nvm）
├── scripts/rotator.py
└── templates/

~/.config/cswap-rotator/
├── config.json                         # 你的配置
└── .last-check.json                    # 自适应轮询的 cache

~/Library/LaunchAgents/com.cswap-rotator.plist   # launchd 注册项
~/Library/Logs/cswap-rotator.log                  # 每次真检查写一行 JSON
```

---

## 通知

`scripts/rotator.py` 里有个 `notify()` 函数，目前只往 log 里
写一行 `{"notification":...}` 留痕。想接飞书 / Slack / macOS 通知 / 邮件，
改这一个函数就行。


