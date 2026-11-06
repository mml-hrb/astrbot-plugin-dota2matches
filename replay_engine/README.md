# 录像解析引擎的 Java 源码

这里放的是**本插件自己写的**那几个 Clarity 导出器的源码。运行时 `dota_replay_parse.py`
会以子进程方式调用引擎 `classes/` 里的类来读录像。

## 为什么只有这一个文件

引擎里其余导出器（`Run` / `Snap` / `Position` / `Eco`）用的是社区现成构建，
随引擎一起以 `.class` 分发，源码不随本仓库走 —— 我们要用它们的**行为**，
不需要改它们的代码。

`WinCurve` 不一样：它是为本插件新加的（胜率曲线，判关键团用），
市面上没有现成的，**没有源码就重建不出来**。2026-11-05 就差点丢过一次 ——
当时只在临时目录里留了一份，清理后只剩编译好的 `.class` 在服务器上，
本地连字段名都是从聊天记录里翻回来的。所以放进仓库。

## 编译

在引擎目录下（也就是有 `classes/` 和 `lib/` 的那一层）执行：

```bash
javac -proc:none --release 21 -encoding UTF-8 \
      -cp "classes:lib/*" -d classes WinCurve.java
```

- **`--release 21` 必须写。** 目标机的 JRE 往往正好是 21，用更高版本的 JDK
  编出来会 `UnsupportedClassVersionError`，而这个错误发生在子进程里，
  插件侧只会看到「解析失败」四个字。
- `-proc:none` 关掉 Clarity 自带的注解处理器，否则每次编译会刷一屏警告
  （不影响产物，只是吵）。
- Windows 下 `-cp` 的分隔符是 `;` 不是 `:`；用 Git Bash 时会踩这个坑。

## 部署

编出来的 `WinCurve.class` 放进引擎的 `classes/`，和其他导出器并排：

```
<引擎目录>/
├── classes/
│   ├── Run.class
│   ├── Snap.class
│   ├── ...
│   └── WinCurve.class      ← 这个
└── lib/
    ├── clarity-5.0.1.jar
    └── ...
```

⚠️ **别用「战报能正常出」来判断它装好了。** 缺 `WinCurve.class` 时插件不会报错，
只是每份战报都静默少掉「胜负走势」和「阵容先验」两段（`_win_curve_block` 有兜底，
拿不到就返回空）。部署完单独核对一次：

```bash
ls <引擎目录>/classes/WinCurve.class
```

## 输出契约

导出的东西是一行带 `WINCHANCE:` 前缀的 JSON（前缀是为了和 slf4j 的日志分开，
`run_java` 把 stderr 并进了 stdout）：

```json
{"start": 105.83, "end": 1338.83,
 "chance": [64 个 0~100 的整数],
 "radiant": [同上], "dire": [100 - chance],
 "series": [[demo 秒, 0~1 的浮点], ...]}
```

两个量纲**不一样**，这是刻意的，也是踩过的坑：

| 字段 | 量纲 | 谁在用 |
|---|---|---|
| `series` | **0~1** | `_curve_points` 会乘 100 显示 |
| `chance` / `radiant` / `dire` | **0~100** | 直接用 |

`series` 如果错写成 0~100，报告上会出现「5400% 胜率」——**能跑通、不报错、数值全错**。
改这个文件时先把这条对着看一遍。

时间一律是 **demo 时间**（从建图算起，含选人阶段）。换算成比赛时间要减 `end - duration`，
由 Python 侧现算（`dota_replay.align_curve`），不同比赛偏移不一样。
