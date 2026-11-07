# AgentRuntime 运行追踪接入要求

自本次平台更新后的新评测起，三个赛题都要求真实业务使用基础镜像的 AgentRuntime/Telemetry 追踪，并成功导出本轮任务 Trace。原赛题输入输出协议、评分规则、镜像启动命令和工作目录不变。

## 必须满足

- 在真实处理题目的进程初始化 `AgentRuntime`；保留平台注入的 OTLP 地址、队伍和 Agent 身份，不硬编码或关闭导出。
- `/health` 成功时必须返回布尔值 `"telemetry_configured": true`，该值应来自 `runtime.telemetry.export_enabled`，不能固定写 true。健康检查不应调用模型。
- 每次实际处理题目须产生当前任务的 `agent.session` 和其子级 `agent.task`；题号/任务 ID、平台注入身份及时间须与本轮一致。
- 本轮 Gateway 有成功模型调用时，应在对应任务上下文中产生真实 `agent.llm`；通过 SDK 支持的模型客户端调用，不能仅自报调用次数。每题结束后 flush。
- 题目允许零模型调用时，0 次、0 Token、0 模型耗时本身不是错误；仍需记录实际执行任务的 Session/Task。D 类是否另有模型源码要求，继续按对应公开材料执行。

## 自定义接口接入

标准 SDK `/solve` 可直接使用其任务处理流程。赛题一继续接收八字段原始题目 JSON，赛题二继续使用 `/send`、`/result/{task_id}`，不要求改成 SDK 的包装请求。自定义路由可按以下方式包裹真正的处理过程，不能先返回成功再伪造空 Trace：

```python
import asyncio
import os
from agenthack import AgentRuntime
from agenthack.telemetry import task

runtime = AgentRuntime(your_agent)

async def handle_real_task(task_id, request):
    try:
        with runtime.telemetry.session(
            task_id, "contest", os.environ.get("CHALLENGE_SESSION_ID")
        ):
            with task() as span:
                result = await your_real_handler(request)
                span.result(result)
                return result
    finally:
        await asyncio.to_thread(runtime.telemetry.flush)
```

赛题一可用题目 `case_id` 作为上述任务 ID；赛题二发送端与接收端分别以该例 `task_id` 追踪真实工作。后台接收/求解任务也必须进入该上下文；健康路由不等同任务上下文。模型调用仍使用赛事注入配置及会话，不自行接外部模型。

## 报错含义

- `AGENT_RUNTIME_NOT_CONFIGURED`：健康接口未确认运行追踪配置，请检查初始化和布尔字段。
- `AGENT_RUNTIME_TRACE_MISSING`：缺少本轮有效任务追踪，或身份/任务/时间不匹配；先检查埋点和导出，仍异常请提供评测编号让组委会检查采集端。
- `AGENT_RUNTIME_LLM_TRACE_MISSING`：网关已记录成功模型调用，但缺少对应模型追踪；检查调用是否处于真实任务上下文。
- `AGENT_RUNTIME_PLATFORM_CONFIG_INVALID`、`AGENT_RUNTIME_TRACE_COLLECTION_FAILED`：平台采集配置/查询异常，请联系组委会，不必仅因此反复上传镜像。

镜像检查只核验启动与健康声明；真实评测还要核验实际任务 Trace。以前通过检查的镜像也会在实际启动时重新核验。缺失证据时不会发布有效成绩，不自动重算历史评测。

运行追踪、tshark 抓包、Gateway 审计是分别核验的证据。Trace 不能替代 pcap，初始化运行时也不会凭空产生网络流量。平台抓包未启动或无法完整封存时，同样不出成绩。原始 Trace、pcap 和含敏感内容的审计仅供受限管理使用。
