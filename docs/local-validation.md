# 本地验证记录

本记录仅用于工程验收，不是平台评分或 q。输入为自行生成的测试文本，不含赛事题库文章、隐藏问题或答案。模型桩只验证协议与调用流程，不具备正式模型的理解能力与时延。

## 2026-11-08：发送端语义 Agent 与接收端决策

本轮默认 aggressive，另保留 lossless 对照。容器内 61 项测试通过，包含此前模型/协议测试，以及分块恢复、ACK 丢失再查询、累计确认、元数据补发、证据引用验证、数字/否定丢失拒绝、critic 拒绝/超时回退、keep 决策、失败原因反馈后修正、语义信封与正文前缀转义等。

相同的 4,334 字节合成文章包含 18 条预先定义的机构/金额/年份事实，测试结果如下。所有场景均保留了这 18 条已知事实；桩模型会按测试模板提取这些字段，不证明正式文章的事实保持能力。

| 本地场景 | B 字节 | 双向 UDP 入口字节 | 模型桩请求 | 其中 Sender 编码请求 | 用时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| aggressive，核验接受 | 906 | 608 | 6 | 3 | 约 0.64s |
| lossless，对照 | 904 | 730 | 5 | 0 | 约 0.46s |
| aggressive，critic 拒绝，回退无损 | 904 | 730 | 9 | 3 | 约 0.77s |
| aggressive，额外强制丢失首个数据和 ACK | 906 | 1,234 | 6 | 3 | 约 3.94s |

计数说明：Sender 编码请求不包括其后全文 critic。桩模型没有真实推理时间，样本高度重复，完整压缩和语义压缩都很容易；这些数值不能预测正式 Gateway 的 Token、时延、q 或总分。最后一例共丢弃 5 个报文，其中数据 1 个、ACK 2 个，最大数据报 251 字节，任务仍完成。测试有 12% 随机丢包、10–70ms 延迟、重复及确定性故障注入，不是官方相关丢包/TBF 条件。

```bash
python3 tools/docker_smoke.py --tag candidate --semantic-mode aggressive
python3 tools/docker_smoke.py --tag candidate --semantic-mode lossless
python3 tools/docker_smoke.py --tag candidate --semantic-mode aggressive --critic-reject
python3 tools/docker_smoke.py --tag candidate --semantic-mode aggressive --drop-data-ack
```

正式提交前仍检查 Linux/AMD64、非 root、两端 health 和提交归档清单。平台注入的 Sender 模型权限/配置、真实慢网关并发和隐藏题损失需要下一轮正式评测；若 Sender 未获模型配置，程序会回退到无损数据传输，Receiver 仍须调用模型。

## 2026-11-07：并行事实记录版本

- 运行环境：Docker Desktop、Linux/AMD64、官方 `agenthack/base:1.1.1`，容器以 `agent` 运行。
- 模型流程：分段并行提取、独立全文覆盖候选、局部补丁复核；没有失败段原文回传。
- 容器内单测：29 项通过。覆盖截止预算、模型输出截断与空值、连接错误重试、部分慢请求/全部失败、照抄拒绝、较长事实补丁、迟到完整文章改进、Trace 导出顺序、UDP 帧与无损压缩。
- 本地集成：`python3 tools/docker_smoke.py --tag candidate`。使用独立 Docker 网络、本地 HTTP 模型桩、双向 UDP 中继；设置 12% 随机丢包、10–70ms 转发延迟及 3% 重复，可能乱序。该条件不等于官方 TBF/netem 或相关丢包模型。
- 一次实测：测试原文 4,334 字节，结果 904 字节；模型桩成功处理 5 次 SDK 请求；中继累计入口 UDP 3,250 字节，最大报文 237 字节，丢弃 2 个报文；用时约 0.69 秒。该合成文本重复多、模型桩无实际推理耗时，不能外推正式压缩率或耗时。
- 测试预先定义的 18 条机构/金额/年份事实全部出现在最终模型记录中；这不是隐藏问题 q。
- 测试容器未注入赛事 OTLP，不能验证平台 Trace 接收。已单测确认 success 在 task/session 结束及 flush 后公布，真实平台证据仍以正式评测为准。

## 复现

```bash
docker run --rm --platform linux/amd64 --entrypoint python \
  -v "$PWD/tests:/tests:ro" semantic-agent-receiver:dev \
  -m unittest discover -s /tests -v
python3 tools/docker_smoke.py --tag dev
```

`tests/smoke_fixture.py` 和开发工具不在 Dockerfile 的 COPY 列表中，提交镜像只包含运行时代码。提交前执行 `python3 tools/export_submission.py`，最终上传文件与提交号、哈希对应关系以 `dist/submission-manifest.json` 为准。
