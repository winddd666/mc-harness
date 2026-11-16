这是 2026-11-09 对比赛 API 的实测记录。先看 [REPORT.md](REPORT.md)，完整请求和测量在 [results.jsonl](results.jsonl)。

运行需要 Python 3.10+ 和 httpx。脚本从环境变量 NJU_API_KEY 读取密钥，密钥不在这些文件中。

```sh
python3 -m pip install httpx
read -rs 'NJU_API_KEY?API key: '; export NJU_API_KEY
python3 benchmark.py all
unset NJU_API_KEY
```

上面的 read 写法适用于 zsh。也可以通过你已有的密钥管理工具注入 NJU_API_KEY。

可以单独运行 pilot、perf、compatibility、knowledge、agents、context。补充实验为 supplement.py、format_controls.py、recovery.py。默认模型为 default，NJU_BASE_URL、NJU_MODEL、NJU_BENCH_OUT 可覆盖配置。

当前 benchmark.py 增加了每进程 24 次 / 60.5 秒的滑动窗口，NJU_RPM_BUDGET 可修改；NJU_MIN_REQUEST_INTERVAL 可额外设置最小请求间隔。所有线程共用窗口。若同时启动多个进程或其他队伍成员共用密钥，需要另做全局配额协调。不要并行运行多份脚本。

原始测量在发现服务端限流前没有客户端节流，出现了两次429；recovery.py 的原始补测采用2.6秒最小间隔。更新后的脚本将客户端排队时间单独记录为 client_rate_wait_s，API延迟从实际发送请求开始计算；批次吞吐包含完整批次墙钟时间。保存的原始 results.jsonl 保留当时测量，不含后来新增的等待字段。

perf 阶段有意把生成限制为256 token，所以 finish_reason=length 是预期现象；pilot_tool_auto 和 control_fuel_thinking 的 length 表示真正耗尽思考预算而没有完成输出。

运行离线判分器校验无需密钥或httpx：

```sh
python3 validate_harness.py
python3 summarize.py
```

summarize.py 需要 matplotlib，读取当前目录中的原始结果并重新生成报告、汇总和图表。模拟器是简化的文本环境：失败动作被拒绝，不会真的破坏矿物；熔炼同步完成，不模拟tick、延迟、寻路物理或怪物。配方和工具前置条件已经在系统提示中给出。实际Minecraft错误动作的代价会更高。

测试套件没有接入真实Minecraft实例，也不包含真实截图识别、战斗、长时间生存、跨服或比赛harness集成。

