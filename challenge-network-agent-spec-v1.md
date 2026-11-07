# 赛题2参赛者 Agent 输入输出与网络接入规范

> 本文规定参赛者镜像如何启动、如何连接平台网络仿真器、如何接收任务、如何发送数据以及如何返回最终结果。
>
> 本规范是赛题二网络仿真器连接和 Agent 接口的完整公开说明。

## 1. 运行形态

平台为每个评测用例启动：

```text
Sender 容器  <-> 发送内部网络  <-> 可信 network-emulator  <->  接收内部网络  <-> Receiver 容器
```

Sender 和 Receiver 不在同一内部网络，不能直接访问对方容器名或容器 IP。仿真器负责双向 UDP 转发、限速、延迟、抖动、丢包、乱序和重复。

参赛队应提交两个独立镜像：`sender-image` 和 `receiver-image`。除非组委会后续明确允许单镜像双角色，否则不接受用一个镜像替代两份角色镜像。

## 2. 容器通用要求

| 项目        | 要求                                           |
| --------- | -------------------------------------------- |
| 目标架构      | `linux/amd64`，除非平台另行公告                       |
| 用户权限      | 非 root                                       |
| HTTP 监听地址 | `0.0.0.0:8080`                               |
| 健康检查      | `GET /health`                                |
| 数据协议      | UDP，使用平台注入的对端地址和端口                           |
| 宿主机端口     | 不需要发布                                        |
| 禁止能力      | `--privileged`、`CAP_NET_ADMIN`、`CAP_NET_RAW` |
| 网络配置      | 不得执行 `tc`，不得修改路由、qdisc、防火墙或虚拟网卡              |
| 外部访问      | 禁止公网、外部存储和外部模型                               |
| 工具        | 建议保留 `curl`，便于平台健康检查                         |

平台会注入：

```text
NETWORK_ROLE=sender      # Sender 容器
NETWORK_ROLE=receiver    # Receiver 容器
```

当前一轮测评统一使用 3 篇固定的内部文章，每篇分别运行好、中、差 3 档信道，共 9 个实例。所有队伍使用相同的文章集合，文章不来自公开网络资源。文章内容只在对应实例中下发给 Sender。

## 3. 选手可读取的数据与路径边界

参赛镜像不得依赖赛事服务器上的固定文件路径。平台不会要求选手预先挂载题库、问题、参考答案或评测配置；正式运行时，Sender 通过 `POST /send` 请求体中的 `payload_b64` 获取当前文章 A。

选手程序允许读取的输入只有：

| 输入       | 获取方式                                    | 说明                                        |
| -------- | --------------------------------------- | ----------------------------------------- |
| 文章 A     | Sender 的 `POST /send` 请求体 `payload_b64` | UTF-8 字节的 Base64 表示，Sender 可在容器内临时保存或直接处理 |
| 当前任务参数   | 同一个 `POST /send` 请求体                    | `task_id`、`chunk_size`、`deadline_ms`      |
| 网络对端参数   | 平台注入的环境变量                               | `UDP_PEER_HOST`、`UDP_PEER_PORT`           |
| 统一模型接入信息 | 以另行发布的统一接入文档为准                          | Receiver 按统一文档完成模型调用适配                    |

以下内容对参赛镜像不可见，也不得通过路径、环境变量、网络或旁路访问：

- 服务器题库目录、文章源文件和题库配置；
- 隐藏问题 Q、参考答案和评分规则；
- 平台内部结果目录、评测日志和榜单数据；
- 平台评测程序及其配置文件。

因此，文档中不约定 `/data`、`/题库`、`/config` 等选手文件路径。若平台未来提供额外只读挂载，必须在冻结版公告中明确挂载点、文件格式和权限；在此之前，选手不得假设这些路径存在。

## 4. 网络仿真器连接

### 4.1 唯一数据面地址

```text
UDP_PEER_HOST=emulator
UDP_PEER_PORT=9000
```

Sender 把 UDP 数据报文发送到 `emulator:9000`。Receiver 在本地 `0.0.0.0:9000` 监听仿真器转发的数据；Receiver 的 `UDP_PEER_HOST` 主要用于发送 ACK、NAK 或补料请求。

不要将 `emulator` 替换为宿主机公网 IP、平台 API 地址、Sender IP、Receiver IP 或固定容器 IP。

### 4.2 数据报文约束

平台不规定应用层报文格式。参赛者自行决定分片格式、任务 ID、序号、校验字段、ACK/NAK、重传、FEC、纠删码和语义补料策略。建议每个分片携带至少：`task_id`、协议版本、分片序号、总片数或结束标识、负载长度和校验信息。

应用层报文不得假设 UDP 一定可靠、有序或不重复。反馈、重传和冗余也会经过弱网并占用带宽。

平台只通过 HTTP 向 Sender 下发任务，不会另行把文章 A 下发给 Receiver。Receiver 必须从收到的 UDP 应用层报文中识别 `task_id`、分片关系和结束条件。若 Receiver 的超时或恢复策略需要 `deadline_ms` 等任务参数，Sender 应将所需参数放入自定义 UDP 协议；平台不会为 Receiver 额外提供共享文件或旁路任务通道。

## 5. Sender 输入输出接口

### 5.1 健康检查

```http
GET /health
```

成功响应必须为 HTTP 200 和 JSON：

```json
{
  "status": "ok",
  "role": "sender"
}
```

### 5.2 任务下发

```http
POST /send
Content-Type: application/json
```

请求：

```json
{
  "task_id": "eval-20261005-1a2b3c4d",
  "payload_b64": "<文章A的UTF-8字节经过Base64编码>",
  "chunk_size": 512,
  "deadline_ms": 150000
}
```

字段要求：

| 字段            | 类型      | 必填  | 含义                             |
| ------------- | ------- | ---:| ------------------------------ |
| `task_id`     | string  | 是   | 当前用例唯一标识，必须原样带入数据协议            |
| `payload_b64` | string  | 是   | 文章 A 的 UTF-8 Base64 表示         |
| `chunk_size`  | integer | 是   | 平台下发的建议应用层数据分片大小；当前固定为 512 字节，不限定自定义报文格式 |
| `deadline_ms` | integer | 是   | 当前用例的发送截止时间                    |

Sender 应尽快完成字段校验并返回，不要等待所有 UDP 数据发送完成：

```http
HTTP/1.1 202 Accepted
Content-Type: application/json
```

```json
{
  "accepted": true,
  "task_id": "eval-20261005-1a2b3c4d"
}
```

收到任务后，Sender 应完成 Base64 解码、UTF-8 校验、分片、通过 `UDP_PEER_HOST:UDP_PEER_PORT` 发送，并在 `deadline_ms` 内按协议处理反馈、冗余、重传和终止。`chunk_size=512` 是当前建议值，Agent 可按自己的压缩、FEC 和报文协议采用其他分片大小。

完整 UDP 应用数据报不得超过 1400 字节。该长度包含参赛者自定义协议头、任务标识、序号、校验信息和实际数据，不包含 IP 头与 UDP 头。超过 1400 字节的数据报不会被可信中继转发。Agent 应根据协议头的实际长度计算可用数据空间，并为可变字段留出余量。

拒绝请求时返回 4xx 和结构化错误，例如：

```json
{
  "accepted": false,
  "task_id": "eval-20261005-1a2b3c4d",
  "error": "invalid_payload_base64"
}
```

## 6. Receiver 输入输出接口

### 6.1 健康检查

```http
GET /health
```

成功响应必须为 HTTP 200 和 JSON：

```json
{
  "status": "ok",
  "role": "receiver"
}
```

### 6.2 UDP 接收

Receiver 必须监听：

```text
0.0.0.0:9000/udp
```

Receiver 应按 `task_id` 保存已收到的分片，并处理乱序、重复、丢包、校验失败和截止时间。到达截止时间时应停止无限等待，生成当前最佳 B 或返回未完成状态。

Receiver 不会收到单独的文章输入或任务下发 HTTP 请求。平台在向 Sender 下发任务后，直接轮询 Receiver 的 `/result/{task_id}`；因此 Receiver 应允许平台在首个 UDP 分片到达前查询该接口，并返回 `pending`。

### 6.3 统一模型使用

Receiver 需要使用赛事统一模型辅助生成最终语义报文 B。具体接入方式、使用规则和资源限制均以组委会另行发布的《统一模型接入说明》为准。参赛者不得连接未经授权的外部模型服务。

### 6.4 结果轮询

平台轮询：

```http
GET /result/{task_id}
```

未完成示例：

```json
{
  "task_id": "eval-20261005-1a2b3c4d",
  "status": "pending",
  "received_chunks": [0, 2],
  "missing_chunks": [1]
}
```

尚未收到该 `task_id` 的任何 UDP 报文时，也应返回 HTTP 200 和 `status="pending"`，不得因任务尚未建立而返回 404。

完成示例：

```json
{
  "task_id": "eval-20261005-1a2b3c4d",
  "status": "success",
  "reconstructed": {
    "content": "接收端实际生成的语义报文 B",
    "format": "text"
  },
  "received_chunks": [0, 1, 2],
  "missing_chunks": []
}
```

字段要求：

| 字段                      | 要求                                         |
| ----------------------- | ------------------------------------------ |
| `task_id`               | 必须与当前请求任务 ID 完全一致                          |
| `status`                | `pending`、`processing`、`success` 或 `error` |
| `reconstructed.content` | `success` 时必填，类型为 UTF-8 字符串，是平台评测的唯一 B 内容  |
| `reconstructed.format`  | 可选；默认 `text`。本版本不接受对象序列化后冒充 B              |
| `received_chunks`       | 已接收分片序号，仅用于诊断                              |
| `missing_chunks`        | 当前缺失分片序号，仅用于诊断                             |
| `error`                 | `status=error` 时建议提供的简短错误码或说明，不得包含敏感信息     |

平台将 `reconstructed.content` 的 UTF-8 字节视为最终语义报文 B，并在 Receiver 提交后完成评测。参赛者无需实现其他结果提交端点，不得在结果中返回隐藏问题、参考答案、自报分数或其他平台内部信息。

题库文章的固化文本（包括平台发送的标题、来源说明和正文）构成 Sender 收到的文章 A；压缩率分母由平台按实际发送的 UTF-8 字节数计算。参赛者无需读取服务器题库文件或维护题库索引。

基础镜像包如另有通用 `POST /solve` 接口，本赛题评测不会调用该接口：Sender 使用本节的 `POST /send`，Receiver 使用 UDP 9000 和 `GET /result/{task_id}`。参赛镜像可以保留 `/solve` 以兼容基础镜像，但其响应不参与赛题评分。

## 7. 当前执行门限

| 项目 | 当前门限 |
| --- | ---: |
| Sender、Receiver 容器启动及 `/health` 探活 | 每个容器 180 秒 |
| 单次 `/health`、`/send`、`/result/{task_id}` 控制请求 | 10 秒 |
| 好信道单实例截止时间 | 120 秒 |
| 中信道单实例截止时间 | 150 秒 |
| 差信道单实例截止时间 | 180 秒 |

Receiver 的单次 `/result/{task_id}` 查询超时后，只要当前实例尚未达到对应信道的截止时间，平台仍会继续轮询。Agent 应根据 `deadline_ms` 提前完成降级处理和最终 B 生成，不应把全部时间用于等待分片或模型响应。时间参考公式、实例硬截止、官方语义裁判和运行证据采集门限见《赛题2测评超时门限说明》；信道参数见《赛题二输入、输出与信道参数说明》。

## 8. 平台注入但禁止自行设置的变量

以下变量由平台或可信仿真器管理，参赛者不得依赖固定值、篡改或写入镜像配置：

```text
SENDER_IP
RECEIVER_IP
MAX_UDP_BYTES
NETWORK_PROFILE
NETEM_RATE_KBIT
NETEM_DELAY_MS
NETEM_JITTER_MS
NETEM_LOSS_PERCENT
NETEM_REORDER_PERCENT
NETEM_SEED
```

平台会创建和销毁内部网络、启动和配置仿真器、注入任务 ID、统计运行结果并清理临时资源。参赛者不需要下载、构建、启动或修改仿真器。

## 9. 本地开发接口

本地至少验证：

```bash
curl http://127.0.0.1:8080/health
curl -X POST http://127.0.0.1:8080/send \
  -H 'Content-Type: application/json' \
  -d '{"task_id":"local-1","payload_b64":"...","chunk_size":512,"deadline_ms":150000}'
curl http://127.0.0.1:8080/result/local-1
```

上述三个命令需要分别访问对应角色容器的 HTTP 端口；Sender 和 Receiver 同时在本机运行时，应映射到不同的宿主机端口。正式评测中无需发布宿主机端口，平台会直接访问容器内的 `8080/tcp`。

本地直连或自建 UDP 中继不能代表正式 TBF/netem 条件。正式评测不使用参赛者自带的网络仿真器配置。

## 10. 常见错误

| 错误                       | 后果                  |
| ------------------------ | ------------------- |
| 将 `emulator` 改为公网或宿主机 IP | 无法进入平台弱网链路          |
| Sender 直接连 Receiver      | 连接失败或绕过仿真器          |
| 把 UDP 9000 当 HTTP 端口     | 数据协议错误              |
| 监听 `127.0.0.1:8080`      | 平台无法健康检查            |
| `/health` 只返回 SDK 默认 `healthy`，没有赛题要求的 `status` 和 `role` | 镜像检查或运行探活失败 |
| Receiver 直接拼接或返回未验证数据    | 不能形成有效最终结果          |
| Receiver 未按统一模型接入说明进行调用  | 调用失败或不符合赛事要求        |
| 在镜像中执行 `tc`              | 违反权限和公平性要求          |
| 返回原文、隐藏问题、答案或自报分数        | 违反黑盒评测边界            |
| 读取或依赖平台题库、评测程序或结果目录等固定路径 | 正式容器不提供这些路径，且属于越权访问 |

## 11. 提交前检查

- [ ] 镜像为平台要求的架构并以非 root 运行。
- [ ] HTTP 服务监听 `0.0.0.0:8080`。
- [ ] Sender 的 `/health` 返回 HTTP 200 和 JSON `status=ok、role=sender`。
- [ ] Receiver 的 `/health` 返回 HTTP 200 和 JSON `status=ok、role=receiver`。
- [ ] Sender 实现 `/send`，校验输入后快速返回 `202`。
- [ ] Sender 使用平台注入的 `UDP_PEER_HOST` 和 `UDP_PEER_PORT`。
- [ ] Receiver 监听 UDP `0.0.0.0:9000`。
- [ ] Receiver 可处理丢包、乱序、重复、缺片和校验失败。
- [ ] Receiver 在尚未收到首个分片时，查询 `/result/{task_id}` 仍返回 HTTP 200 和 `pending`。
- [ ] Receiver 已按另行发布的统一接入文档完成模型调用适配。
- [ ] Receiver 实现 `/result/{task_id}` 并返回合法 JSON。
- [ ] 未使用 `NET_ADMIN`、`NET_RAW`、`--privileged` 或自定义 `tc`。
- [ ] 未写入固定容器 IP、未经授权的外部服务地址或隐藏数据。
- [ ] 未依赖服务器题库、评测程序或结果目录的文件路径。
