# 本地验证记录

本记录仅用于工程验收，不是平台评分或 q。输入为自行生成的测试文本，不含赛事题库文章、隐藏问题或答案。模型桩只验证协议与调用流程，不具备正式模型的理解能力与时延。

## 2026-11-08：完整来源与并行候选版本

根据 `3eadc2b` 的正式 39.4 分报告调整后，使用实际构建的 Linux/AMD64 candidate 镜像运行 105 项单测，全部通过。新增覆盖：默认发送原文且不等待模型；已编码输入不再按比例摘要；紧凑生成/审阅挂起时采用已完成覆盖；显式拒绝不发布该候选；完整备用覆盖不被慢分段拖住；晚到全文升级挂起后取消并保留部分 B；bz2/lzma 输出和内存限制、尾随/截断/拼接流拒绝及乱序恢复。

同一 4,334 字节合成来源的四种容器闭环均通过，18 条预定义测试事实全部出现。通过真实 OpenAI SDK 调用本地 HTTP 桩，经带丢包、乱序与重复的 UDP 中继传输；桩无真实理解能力。

| 场景 | B 字节 | 双向 UDP 字节 | 完成桩请求 | Sender 请求 | 用时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 默认 lossless，审阅接受 | 906 | 730 | 6 | 0 | 0.46s |
| 显式 aggressive，审阅接受 | 906 | 608 | 7 | 3 | 0.76s |
| 默认 lossless，审阅拒绝且强制丢失数据/ACK | 1,129 | 1,474 | 6 | 0 | 3.66s |
| 默认 lossless，审阅挂起 60s | 1,129 | 730 | 5 | 0 | 7.01s |

挂起场景在 45 秒任务截止前使用覆盖结果完成，未等待 60 秒审阅响应。丢包场景实际丢失数据 1 个、ACK 2 个，最大数据报 367 字节。其余计数和时延为本地观测，不等同正式 Gateway 成功调用与计费口径。

无损算法对高重复合成事实文本的帧计划比较：中文 9,632 字节从旧 zlib 计划 1,463 降至 1,095 字节；英文 13,649 字节从 1,848 降至 1,375 字节。只证明选择器能利用其他通用无损算法，不代表真实题库 D/A 或分数。

```bash
python3 tools/docker_smoke.py --tag candidate
python3 tools/docker_smoke.py --tag candidate --semantic-mode aggressive
python3 tools/docker_smoke.py --tag candidate --critic-reject --drop-data-ack
python3 tools/docker_smoke.py --tag candidate --slow-review
```

本轮正式分数未知；未通过本地环境验证赛事 OTLP 接收和隐藏问题。历史最高分与已测退化版本均保留，下一次平台报告需绑定本次导出清单的源码提交。

## 2026-11-08：受约束语义编码与低调用接收流程

官方基础镜像 Python 3.11 容器内 91 项单测通过。覆盖 Sender 编码成功/超时回退、精确 UDP 预算、Receiver 压缩候选审阅及忠实兜底、数字与否定检查、部分文章截止处理、完整文章迟到升级、数据/ACK 丢失、累积确认、摘要和帧校验。数字缺口由模型审阅，模型误判时仍可能遗漏重要数字，测试不能证明语义正确率。

4,334 字节合成文章含 18 条固定测试事实。Docker smoke 均通过，模型桩会按测试模板提取事实，不证明正式文章的 q。aggressive 成功路径消耗 6 次桩请求（3 次分段编码、1 次 Sender critic、1 次 Receiver 压缩、1 次 Receiver critic）；正常模型阶段因并发而不等同 Gateway 的计费口径。

| 本地场景 | B 字节 | 双向 UDP 字节 | 模型桩请求 | Sender 模型请求 | 用时 |
| --- | ---: | ---: | ---: | ---: | ---: |
| aggressive，审核接受 | 981 | 608 | 6 | 3 | 约 0.63s |
| lossless 对照 | 906 | 730 | 2 | 0 | 约 0.47s |
| aggressive，Sender critic 拒绝并回退 | 1,129 | 730 | 10 | 3 | 约 0.74s |
| aggressive，强制丢失首个数据及 ACK | 981 | 1,234 | 6 | 3 | 约 3.88s |

这个本地桩样本中 aggressive 的 UDP 量更低，但 B 比 lossless 长，不能宣称综合收益。桩模型无真实推理延迟，文章重复度高；表中数字不可外推到官方模型、题库、Token、q 或总分。丢失场景共丢弃 5 个报文，其中数据 1 个、ACK 2 个；最大数据报 367 字节，任务仍完成。测试网络含 12% 随机丢包、10–70ms 延迟、重复及确定性故障注入，不等于官方相关丢包/TBF 条件。

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
