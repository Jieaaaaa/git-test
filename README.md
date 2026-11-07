# 惜字如金：黑盒弱网语义通信 Agent

本仓库用于实现赛题二的 Sender 与 Receiver。目标是在严格遵守数据边界和容器要求的前提下，让 Receiver 根据弱网实际收到的信息生成可回答隐藏问题的短文本 B，并兼顾正确率、响应时间、文本压缩和通信开销。

## 长期开发原则

**兼顾分数和创新性，要尽可能调用大模型来解决问题，不要为了调用而调用，而是通过大模型提升 Agent 的能力。**

设计与优化时，优先考虑让大模型承担有实际价值的文章理解、事实提取、语义编码、查漏纠错和策略决策。每次调用应有明确目标，并通过评测或消融实验验证其对准确率、鲁棒性或综合得分的贡献；合理分配截止时间与 Token 预算，不以减少模型调用次数作为独立优化目标。

## 当前状态

- 已实现首版 Sender/Receiver 服务、UDP 帧协议和 Dockerfile；Docker Desktop 上的 Linux/AMD64 构建、容器健康检查及本地 HTTP/UDP 多分片闭环已通过。启动阶段现会后台重试 UDP peer DNS，避免平台 DNS 初始化延迟导致进程退出。官方弱网仿真和平台模型/遥测注入验证仍待完成。
- 赛事要求提交两个独立的 Linux/AMD64 OCI/Docker 镜像：Sender 和 Receiver。基础镜像不能直接提交。
- 实现时应以平台当前冻结版规则、实际注入参数和官方 SDK 为准。规则有更新时，先更新本文的版本记录和本节要点。

当前代码位于 `agent_app.py`、`wire.py` 和 `model_policy.py`。首版通过 CRC32 校验分片、SHA-256 校验完整文章，Sender 使用 16 片窗口与 SACK 位图选择性重传，并对每片独立执行无损 zlib 压缩（仅压缩后更小时才采用）；Sender 和 Receiver 分别限制 15 MiB 与 1 MiB UDP 载荷，合计不超过单例 16 MiB 上限。Receiver 先调用模型生成可回退的紧凑事实文本，再利用剩余截止预算调用模型逐项核对并尝试压缩；二次调用失败或超时时返回首个成功模型输出。首次请求失败时按实时剩余预算重试，避免固定等待上限。

协议单测可在项目根目录运行：

```bash
python3 -m unittest discover -s tests -v
```

镜像构建需在已导入 `agenthack/base:1.1.1` 的 Linux/AMD64 Docker 环境执行：

```bash
docker build --platform linux/amd64 --build-arg AGENT_ROLE=sender -t semantic-agent-sender:dev .
docker build --platform linux/amd64 --build-arg AGENT_ROLE=receiver -t semantic-agent-receiver:dev .
```

若没有容器引擎，可基于工作区内的官方 OCI 基础镜像组装两个 OCI 归档：

```bash
python3 build_oci_images.py
```

归档生成在 `dist/semantic-agent-sender.oci.tar` 和 `dist/semantic-agent-receiver.oci.tar`。该路径会校验镜像索引、层摘要、AMD64 架构、非 root 用户和角色配置；它不能替代平台弱网端到端测试。

平台上传用的标准 Docker image TAR 已从实际构建镜像导出：`dist/sender-image.tar` 和 `dist/receiver-image.tar`。两个文件分别对应 Sender 与 Receiver，单独上传，不要再套 ZIP。

Docker Desktop 验证命令：

```bash
export PATH="$HOME/.docker/bin:$PATH"
docker buildx build --platform linux/amd64 --build-arg AGENT_ROLE=sender --load -t semantic-agent-sender:dev .
docker buildx build --platform linux/amd64 --build-arg AGENT_ROLE=receiver --load -t semantic-agent-receiver:dev .
```

本地 smoke test 使用单独 Docker 网络直连 Sender/Receiver，检查了 `/health`、`POST /send`、UDP 分片接收及结果状态。测试容器没有模型网关凭据，因此生成阶段按预期返回 `model_call_failed`；它验证数据面闭环，不代表授权模型调用或正式弱网结果。

## V3.4 成绩诊断

历史首轮自动分为 24.3/100，语义正确率 38.9%；当时 OA03 三个信道均出现模型调用失败。之后 `824430d` 在相同规则 V3.4 下测得 40.7/100，q 提升到 86.7%，但所有成功例 B 都比 A 长，压缩分为 0，且 OA03-good 仍在 120 秒截止时返回 `model_call_failed`。

| 评测编号 | 镜像代码提交 | 自动分 | q | 正确率分 | 时间分 | 压缩分 | 通信分 | 主要异常/短板 |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| 编号未提供（首轮） | 旧版镜像 | 24.3/100 | 38.9% | 11.8/30 | 2.4/10 | 5.9/30 | 4.3/30 | 三个 OA03 均模型调用失败；其他例事实遗漏多 |
| `eval-20261107-e1272196` | `824430d` | 40.7/100 | 86.7% | 27.3/30 | 3.4/10 | 0/30 | 10/30 | OA03-good 返回 `model_call_failed`，0 分 |

`eval-20261107-e1272196` 完成 9/9 判题用例，Gateway LLM 调用记录为 9，总耗时 25m27s。8 个成功 B 的 `rB` 为 100.9%–112.5%，因此压缩分全为 0；`FT` 为 0.336–0.386。该测试编号对应的改进方向是保住高 q、显著缩短 B，并处理慢网关调用造成的 OA03 截止异常。40.7 是自动分，尚非最终复核成绩。

当前实现把模型预算绑定真实剩余 deadline：首调先在动态预算内直接生成可用 B；首调失败时用新的剩余预算重试，第二次原文核对/压缩只作为可选改进，失败就保留首调结果。首调使用 68% 的剩余时限，重试使用届时全部剩余时限；可选复核至多使用其开始时剩余时间的一半，均保留锁定余量，不用固定 70/40 秒等待。首调输出要求保留事实并用紧凑分号/标签表达，复核只有在输出更短时才替换首调候选。此版本尚未获平台复测，提示中的 80% 是软目标，不能保证实际 `rB < 100%`。

## 评测流程

```text
平台 POST /send（Sender 收到完整文章 A）
  -> Sender 编码并经 UDP 中继发送
  -> Receiver 只从 UDP 分片恢复信息并调用统一模型
  -> 平台轮询 GET /result/{task_id}，锁定 reconstructed.content 为 B
  -> 平台使用隐藏问题评判 B
```

Sender 与 Receiver 分处隔离网络，不能直接互访。文章 A 只能经比赛 UDP 数据面传递；Receiver 不得从请求、挂载目录、文件、环境变量或其他旁路获得 A。隐藏问题 Q、参考答案和评分配置对两端不可见。

## 硬性运行要求

| 项目 | 要求 |
| --- | --- |
| 镜像 | Sender、Receiver 两个独立镜像，`linux/amd64`，基于 `agenthack/base:1.1.1` 构建，不以基础镜像直接提交 |
| 用户 | 容器以非 root 用户运行 |
| HTTP | 两端监听 `0.0.0.0:8080`；实现 `GET /health` |
| Sender API | `POST /send`；有效任务尽快返回 HTTP 202，不等待整篇发送完成 |
| Receiver API | `GET /result/{task_id}`；任务尚未到达时也返回 HTTP 200 和 `pending` |
| UDP | Sender 发往平台注入的 `UDP_PEER_HOST:UDP_PEER_PORT`；Receiver 监听 `0.0.0.0:9000/udp` |
| 报文大小 | 每个 UDP 应用数据报不超过 1400 字节，协议头也计入 |
| 单例通信量 | 双向累计 UDP 载荷不超过 16 MiB；数据、头、ACK/NAK、心跳、冗余和重传均计入 |
| 结果 | 成功时返回 UTF-8 字符串 `reconstructed.content`，它是唯一被评分的 B |
| 禁止项 | 禁止公网/未授权外部服务、外部模型、旁路通信、预存答案、特权能力、`CAP_NET_ADMIN`/`CAP_NET_RAW`，以及修改 `tc`、路由、防火墙或虚拟网卡 |

健康检查需返回正确角色，并报告真实遥测状态：`telemetry_configured` 必须取自 `runtime.telemetry.export_enabled`，不能硬编码。健康检查不得调用模型。

## HTTP 接口

Sender 接受的平台请求示例：

```json
{
  "task_id": "eval-example-001",
  "payload_b64": "<文章 UTF-8 字节的 Base64>",
  "chunk_size": 512,
  "deadline_ms": 150000
}
```

校验 Base64 与 UTF-8 后，Sender 应快速返回：

```json
{"accepted": true, "task_id": "eval-example-001"}
```

Receiver 处理中可返回 `pending` 或 `processing`，并附诊断用的分片序号。完成后返回：

```json
{
  "task_id": "eval-example-001",
  "status": "success",
  "reconstructed": {
    "content": "最终语义报文 B",
    "format": "text"
  },
  "received_chunks": [],
  "missing_chunks": []
}
```

`task_id` 必须原样贯穿 UDP 协议和结果 API。UDP 可能丢包、乱序、重复，Agent 必须自行处理去重、校验、完成判定和截止时间。

## 信道、计时与评分

当前工作区规则列出的信道档位如下；具体实例以平台运行时注入的 `deadline_ms` 和冻结配置为准。

| 档位 | 带宽/方向 | 单向时延/抖动 | 丢包率 | 截止上限 |
| --- | ---: | ---: | ---: | ---: |
| 好 | 2,000 kbit/s | 20/5 ms | 0.2% | 120 s |
| 中 | 256 kbit/s | 100/20 ms | 5% | 150 s |
| 差 | 64 kbit/s | 250/60 ms | 12% | 180 s |

双向都受损。好/中/差权重为 20%/30%/50%。新 OA 规则的单例评分为：

```text
q  = 10 道隐藏问题的正确得分 / 10
FT = Tref / (Tref + T)
FB = max(0, (1 - B/A) / (1 + B/A))
FD = 1 / (1 + D/A)
S  = q * (30 + 10*FT + 30*FB + 30*FD)
```

其中 A、B 是 UTF-8 字节数；D 是双向累计 UDP 载荷字节数；T 从派发文章到 B 锁定；新题库 `Tref = max(30, A_bytes/200) + 8*A_bytes/R_bits_per_second`。超时例为 0 分，失败例保留在均分分母中。准确率 q 会同时缩放所有效率项，因此首要目标是保证隐藏问题可答，再在这个前提下压缩 B、减少 D 和 T。

## 统一模型与运行追踪

- Receiver 必须使用赛事授权的统一模型生成 B；每个正常成功例至少需要一次可核验的成功模型调用。
- 通过基础镜像 AgentRuntime 支持的模型客户端调用，使用平台实际注入的模型配置。不得连接未经授权的模型或外部服务。
- 基础镜像声明的网关环境变量为 `PLATFORM_LLM_URL` 与 `PLATFORM_API_KEY`；当前代码读取这两项，也兼容本地 `OPENAI_BASE_URL` / `OPENAI_API_KEY`。模型名读取 `PLATFORM_MODEL_NAME` 或 `MODEL_NAME`，默认 `deepseek_v4`。
- 模型调用应发生在真实任务上下文中，并产生对应 `agent.session`、`agent.task` 和真实 `agent.llm` Trace；每个任务结束后 flush 遥测。
- 自定义 `/send`、后台 UDP 接收/求解和 `/result` 流程均需正确关联 `task_id`。Receiver 可能先收到 `/result` 轮询，再收到首个分片。
- 你在对话中提供了 `deepseek_v4` 及 `http://183.230.58.3:3000/v1` 接入信息。该信息需与组委会实际注入配置核对；配置应从运行时环境读取。
- API Key 是凭据，不写入 README、代码、镜像层、日志或 Git。运行时从平台注入的密钥环境变量读取；本地开发使用未提交的环境配置。当前密钥已在对话中明文提供，建议在平台支持时轮换，并避免再次粘贴或记录。

## 设计目标与创新方向

1. **面向问题的语义保持**：不追求逐字复原 A，而是保留可支持事实、人物关系、时间、因果、数值和限定条件的最小充分表示，减少回答隐藏问题所需的 token。
2. **抗损伤渐进传输**：采用带任务 ID、版本、序号、校验和结束标识的自描述分片；先传高价值核心内容，再补充细节。设计轻量 ACK/NAK、选择性重传或有限纠删冗余，并按信道反馈调整策略。
3. **按评分预算优化**：为模型 token、UDP 字节、重传和截止时间设置预算；对比“多传原文片段”“结构化语义编码”“冗余保护”对 q、FT、FB、FD 的影响。
4. **可回退的生成流程**：Receiver 维护增量恢复状态；达到截止或模型调用接近预算时，基于当前已收信息尽早产出可用 B，避免等待不确定的尾部重传。
5. **可复现实验**：固定文章、信道种子（本地模拟时）、模型参数和版本，分别记录 q、A/B/D、T、FT/FB/FD、Token 与失败原因。以消融实验确认每个机制的净收益。

创新必须通过评分和合规验证：不得依赖题库记忆、平台外知识通道、固定随机种子或隐藏问题信息。

## 后续工作清单

### 阶段 1：规则与运行时基线

- [x] 解包检查基础镜像 SDK/API、默认用户、工作目录、遥测组件和依赖；确认镜像构建约定。
- [ ] 获取并核对正式《统一模型接入说明》、环境变量名、模型请求格式、Token/并发限制及超时策略。
- [ ] 初始化项目结构、配置读取与日志规范；确保密钥不进入源代码、镜像层和日志。

### 阶段 2：端到端合规闭环

- [x] 实现两个容器、非 root 启动、`/health` 和 AgentRuntime 遥测上下文。
- [x] 实现 Sender `/send` 校验、异步接受与 UDP 数据发送。
- [x] 实现 Receiver UDP 监听、按 `task_id` 隔离分片状态、`/result/{task_id}` pending/success/error。
- [x] 完成带任务 ID、协议版本、序号、长度、校验、完成元数据和选择性重传的分片协议，并限制单包大小。
- [x] 在 Receiver 接入统一模型客户端，生成 `reconstructed.content`，并为生成任务 flush Trace。
- [x] 加入单例累计通信量预算封顶和超额停止发送行为；仍需在仿真器中确认统计口径。
- [x] 协议帧编解码、CRC 拒绝、1400 字节上限及 ACK 位图单测通过。
- [x] DNS 暂不可用与恢复场景下两端服务保持运行，恢复后 UDP 路径可用。
- [ ] 用本地双容器/仿真器验证乱序、重复、校验失败、丢包、任务并发、截止和进程重启行为。

### 阶段 3：评分优化与创新验证

- [ ] 建立统一评测脚本，测量 q、B/A、D/A、T/Tref、模型 Token、丢包恢复率和截止成功率。
- [ ] 设计语义压缩编码并比较其与原文分块、摘录、结构化要点等基线的效果。
- [ ] 对比选择性重传、不同 ACK 粒度、有限 FEC 与分层优先级传输，特别关注差档信道的收益和额外 D。
- [ ] 优化 Receiver 增量解码与模型调用时机，在信息足够时尽早锁定高质量 B。
- [ ] 用消融实验选定策略，保留配置、日志摘要和结果表，确认准确率提升没有被时间或通信成本抵消。

### 阶段 4：提交验收

- [x] 本地 Docker Desktop 已按 `linux/amd64` 构建两个镜像，并检查非 root、角色、HTTP 健康接口和启动命令；提交版验收仍待平台验证。
- [ ] 执行好/中/差档完整压力与截止测试，核对 1400 字节单包、16 MiB 通信量、模型调用证据和 Trace。
- [ ] 检查两个镜像不含文章、答案、未授权文件、开发密钥或外部网络依赖；整理提交标签与复现说明。

## 规则来源与需要复核的差异

工作区的详细规则文件：

- `challenge-network-agent-spec-v1.md`：镜像、HTTP、UDP 和数据边界。
- `challenge-network-io-channel-20261026.md`：输入输出、信道参数及计时。
- `challenge-network-description-v2.md`：新 OA 评分公式、通信预算与违规判定。
- `challenge-network-timeout-limits-20261026.md`：控制请求、业务和采集时限。
- `runtime-required-guide.md`：AgentRuntime、Trace 和模型调用核验。

用户提供的平台摘要提到从 A01–A20 随机抽取 3 篇；工作区当前文档则说明当前内部 OA 新评测使用 3 篇固定文章。数据批次可能不同，实施前应以平台活动接口/冻结公告确认。类似地，平台摘要没有完整列出 D、`Tref`、16 MiB 和运行追踪要求，因此实现时需同时满足工作区详细规则和最新平台公告。

## 规则版本记录

- 2026-11-06：根据工作区现有规则文档与用户提供的比赛摘要建立初版；模型凭据不写入仓库。
- 2026-11-06：加入首版 Sender/Receiver、选择性重传协议、Dockerfile 和协议单测；待 AMD64 容器与正式模型网关验证。
- 2026-11-07：Docker Desktop 上完成两端 AMD64 构建、非 root/角色配置检查、health 检查及本地多分片 UDP 闭环；修复了 IPv4 UDP 对端主机名异步解析问题。
- 2026-11-07：修复启动钩子同步解析 UDP peer 导致 DNS 暂不可用时容器退出的问题；验证 peer 长时间不可解析时 health 仍正常，DNS 恢复后 UDP 可继续收发。
- 2026-11-07：根据 V3.4 初赛报告，将模型超时改为截止时间预算、开启 SDK 总预算内重试，并把提示词从选择性摘要改为逐项保留文章事实。
- 2026-11-07：增加逐片无损压缩和模型事实复核调用；首个成功模型输出可回退，避免可选复核超时导致整例无结果。
- 2026-11-07：记录 `eval-20261107-e1272196`（提交 `824430d`，40.7/100，q=86.7%，压缩分=0，OA03-good 模型异常）。
