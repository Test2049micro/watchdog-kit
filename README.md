# watchdog-kit

**Monitoring primitives for one machine's scheduled jobs, built around one
observation: people react to change, not to numbers.**

Four separate watchers on one Mac had grown the same skeleton by hand — fetch →
compare with baseline → classify → persist state — and each had independently
rediscovered the same three failure modes of naive monitoring. This is that
skeleton, extracted and tested.

## The problem this solves

An alert that repeats is an alert that stops being read. That was measured, not
assumed: a scanner reporting `DEGRADED 3` every day became unread within a week —
not because the alert was wrong, but because it did not distinguish *"newly
broken"* from *"broken since March"*.

| bucket | fires? | why |
|---|---|---|
| `NEW` | yes | healthy last round, broken now — the actionable case |
| `CHANGED` | yes | still broken, but differently: a new failure mode is new information |
| `RECOVER` | yes (exit 2) | confirms an intervention worked |
| `PERSIST` | **no** | unchanged: the number was already known, so the message carries nothing |
| `ESCALATE` | yes, once | a PERSIST older than `--escalate-after` (default 7 days), so a long-ignored problem cannot fade out of view by being consistently ignored |

## Three false-positive traps, designed out

Each came from a real mis-diagnosis, and each has a test named after it:

1. **A poll gap is not a liveness signal.** Two rounds 20 seconds apart made every
   interval job on the machine look stale. Liveness comes from a *run counter*
   compared across rounds, and the staleness threshold is `period × slack` —
   never the polling cadence.
2. **A non-zero exit code is static history.** `launchd` reports the last exit
   code, and reading it twice returns the same value twice, which looks exactly
   like "it failed again". A strike only advances on evidence that *changed*: a
   new code, a new attempt, or the same code unresolved past `--escalate-after`.
3. **First observation is never an alert.** Without a previous round, "we have
   never seen this" and "it stopped" are the same reading, so a fresh baseline
   yields `BASELINE` and silence.

Plus a fourth, quieter one: a cadence copied into a config file goes confidently
wrong the day someone edits the plist. `watchdog-kit` infers the period from the
plist itself (`StartInterval`, `StartCalendarInterval`, including arrays of times),
and treats a 0-byte log as its own state (`QUIET`) rather than as "no output, so
fine".

## Install

```bash
pipx install watchdog-kit
```

Python 3.9+, no runtime dependencies, no daemon. Schedule it with launchd or cron
and read the exit code.

## Commands

```bash
watchdog-kit launchd                      # are my scheduled jobs still running?
watchdog-kit launchd --slack 4 --exclude com.apple.
watchdog-kit runs --spec runs.json        # did recorded "completed" runs produce anything?
<your-monitor> --json | watchdog-kit diff # the change layer, on any feed
```

Exit codes are the interface:

| code | meaning |
|---|---|
| 0 | nothing changed. Includes "still broken, already known" — that is what silence means |
| 1 | NEW, CHANGED or ESCALATED: look at this |
| 2 | recoveries only |
| 3 | scan error — unreadable state, missing database, bad spec |

`3` is deliberately not folded into `0`. A monitor that cannot read anything must
not look like a monitor that read everything and found it fine.

## `runs`: auditing an agent's own completion claim

A row that says `completed` is a claim about the past, and asking the model again
does not validate it — published measurements put LLM-judge accuracy on
completion claims close to chance. So this plugin never consults a model. It
contradicts the status column with external signals:

| finding | signal |
|---|---|
| `COMPLETED_EMPTY` | status says done, the result column is null / `""` / `{}` |
| `COMPLETED_THIN` | status says done, the token counter is under `min_tokens` |
| `RUNNING_STUCK` | status says running, untouched for `running_stale_seconds` |

The schema is not assumed — table, column names and thresholds come from a spec
(`examples/specs/generic-runs.json`). Identifiers are quoted and rejected if they
contain anything but `[A-Za-z0-9_]`, because a spec is user-editable config that
is interpolated into SQL. The database is opened `mode=ro&immutable=1`: a monitor
that can write to what it audits can break it.

## Writing a plugin

A plugin is a function returning `Item(id, state, detail)`. The state string is
the equality key for the change layer, so `STALE` and `DEGRADED` are different
news about the same subject, and `state` changing resets the escalation clock.
`plugins/launchd.py` (175 lines) and `plugins/sqlruns.py` (170 lines) are the
reference shapes: read a source, emit items, persist counters in the snapshot dict
the caller hands you.

## Where the overlap with bigger systems is, honestly

| you may know | what it does | what this adds |
|---|---|---|
| Prometheus Alertmanager `for:` / `repeat_interval` | duration-gated, repeating alerts | same idea, per-`id` `NEW/CHANGED/RECOVER/PERSIST` semantics as a Unix filter over any JSON feed, with no server |
| Grafana state history | shows transitions | not a *decision source* for a shell job |
| healthchecks.io / cronitor | "did the job ping?" (needs instrumenting the job) | here, liveness is inferred from `launchd` itself, so unmodified jobs are covered |
| monit / systemd | restart on threshold | explicitly **not** this: `watchdog-kit` observes and classifies. Remediation is deliberately out of scope — a monitor that also fixes things needs a different safety story |
| aide / osquery | host integrity baselining | no drift *alerting*: a change to the baseline updates it quietly, by design |

If you need alerting across a fleet, use Alertmanager. This is for the single
machine whose cron jobs and agent runs nobody is watching, where the cost of
running a server exceeds the value.

## Limitations

- `launchd` support is macOS-specific by nature (`launchctl print`, plist
  semantics). The change layer, strike ledger and `runs` plugin are portable.
- Period inference from `StartCalendarInterval` is an approximation, biased toward
  **longer** cadences so a false positive is late rather than early.
- `COMPLETED_THIN`'s `min_tokens` is a guess about a specific client's effort. It
  is a threshold in your spec, not a constant in code, because the right value
  differs per workload — set it from your own distribution or you will get noise.
- State is a flat JSON file per plugin. It caps at 4000 ids by dropping the least
  recently seen, which re-surfaces a dropped id as `NEW` (loud, not silent).

## Development

```bash
python -m unittest discover -s tests -v     # zero dependencies
```

## 中文摘要

同一台机器上四个自建 watcher 各自手抄了同一套骨架（抓源 → 比基线 → 分级 → 落盘），
并且各自独立踩中同样三个误报坑，这个库就是把骨架抽出来、把三个坑钉成测试。核心主张
是「人只对变化敏感」：`NEW`/`CHANGED`/`RECOVER` 出声，`PERSIST` 静默，持续超过 7 天才
升级一次，避免「天天报同样三个坏」让告警一周内变成背景噪音。三个反直觉设计：判活不能
用轮询间隔（两轮 20 秒会把所有 interval 任务误判 STALE），要看 run 计数增量；launchd 的
`last exit code` 是静止历史，连读两次同码不代表又失败一次；首轮观测一律 `BASELINE` 不告警。
周期从 plist 自己反推（写进配置的周期会在别人改了 plist 之后自信地报错）。`runs` 子命令
审的是 agent 自己声明的「已完成」：不问模型，只看结果列为空、token 低到不合理、running 超过
陈旧窗口这三条外部信号 —— 论文实测 LLM 判「是否假完成」接近随机，所以只能用确定性外部证据。
数据库以只读方式打开、SQL 标识符白名单校验。退出码 0/1/2/3 是接口本身，读不到东西必须报
`3`，不能伪装成「一切正常」。
