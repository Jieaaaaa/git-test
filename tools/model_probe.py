"""Run the real model on a generated, non-contest article without storing credentials."""

import argparse
import asyncio
import getpass
import json
import os
import time
from pathlib import Path

from openai import AsyncOpenAI

from semantic_receiver import generate_adaptive_record
from fact_ledger import parse_groups, render_groups, split_passages
from semantic_agent import merge_grounded_reviews, verify_candidate


def sample_article(stations=8):
    paragraphs = []
    names = ("白石", "青田", "高桥", "南湾", "北原", "远山", "清川", "东浦",
             "临湖", "金谷", "松泽", "西浜", "石岭", "海田", "古桥", "雨林")
    for i, name in enumerate(names[:stations]):
        paragraphs.append(
            "%s研究站于2025年%d月%d日启动第%s组淡水循环试验，负责人为%s。"
            "本组申请经费%d万元，最终批准%d万元，差额由设备租赁费缩减形成，不涉及人员报酬。"
            "试验包含%d台泵，其中%d台作为备用。主泵每日运行%d小时，夜间禁止启动备用泵，"
            "除非水温连续%d分钟高于%d摄氏度且值班工程师签字。"
            "第一阶段样本为%d份，合格率为%d%%；第二阶段在相同温度下仅改变滤芯材质，"
            "样本为%d份，合格率为%d%%，因此报告未将改善归因于温度变化。"
            "设备供应方建议增加药剂，负责人否决该方案，理由是对照组必须保持无添加剂。"
            "本组结果只适用于低盐淡水，不能外推到海水。会议先批准设备检修，再启动复测，"
            "最后封存样本。若复测合格率低于%d%%，须暂停扩建，而不是停止日常监测。"
            "该站与其他站点分别建账，不合并预算，也不交换样本。记录中的全部数值为"
            "本次自行构造的开发测试事实，不代表真实机构、赛事文章或题库。"
            % (name, i % 12 + 1, i + 5, chr(65 + i), name + "工程师", 80 + i * 7, 61 + i * 6,
               9 + i, 2 + i % 3, 8 + i, 11 + i, 31 + i, 150 + i * 13, 71 + i,
               170 + i * 17, 83 + i, 76 + i))
    return "自行构造的研究站比较记录\n\n" + "\n\n".join(paragraphs)


async def main(args):
    key = os.getenv("PLATFORM_API_KEY") or os.getenv("OPENAI_API_KEY") or getpass.getpass("Model API key: ")
    previous = json.loads(args.review_from.read_text(encoding="utf-8")) if args.review_from else None
    source = (previous["source"] if previous else args.source_file.read_text(encoding="utf-8")
              if args.source_file else sample_article(args.stations))
    calls = []
    error = None
    started = time.monotonic()
    async with AsyncOpenAI(base_url=args.base_url, api_key=key, max_retries=0) as client:
        async def request(messages, max_tokens, budget):
            call_started = time.monotonic()
            call = {"role": messages[0]["content"][:65], "max_tokens": max_tokens}
            call["input"] = messages[-1]["content"]
            calls.append(call)
            try:
                async with asyncio.timeout(budget):
                    response = await client.chat.completions.create(model=args.model, temperature=0,
                        messages=messages, max_tokens=max_tokens, timeout=budget)
                choice = response.choices[0]
                call["response"] = choice.message.content
                call["finish_reason"] = choice.finish_reason
                call["usage"] = response.usage.model_dump() if response.usage else None
                if choice.finish_reason == "length" and "FACT_PACKET_V1" in messages[0]["content"]:
                    parse_groups(choice.message.content)
                elif choice.finish_reason != "stop" or not choice.message.content:
                    raise RuntimeError("incomplete_model_response")
                return choice.message.content
            except asyncio.CancelledError:
                call["error"] = "cancelled"
                raise
            except Exception as exc:
                call["error"] = type(exc).__name__
                if isinstance(exc, ValueError):
                    call["reason"] = str(exc)[:120]
                raise
            finally:
                call["seconds"] = round(time.monotonic() - call_started, 2)
        try:
            if previous:
                candidate = render_groups(parse_groups(previous["calls"][0]["response"]))
                parts = await asyncio.gather(*(verify_candidate(scope, candidate, request,
                    started + args.deadline, require_numeric_accounting=True, allowed_source=source,
                    require_evidence=True, return_review=True)
                    for scope in split_passages(source, target_chars=1000, max_sections=3)), return_exceptions=True)
                failures = [part for part in parts if isinstance(part, BaseException)]
                if failures:
                    raise failures[0]
                result = merge_grounded_reviews(source, candidate, [review for _, review in parts])
            else:
                result = await generate_adaptive_record(source, False, started + args.deadline, request)
            status = "success"
        except Exception as exc:
            result, status = "", type(exc).__name__
            error = str(exc)[:160]
    report = {"status": status, "source_bytes": len(source.encode()), "result_bytes": len(result.encode()),
              "ratio": round(len(result.encode()) / len(source.encode()), 4),
              "seconds": round(time.monotonic() - started, 2), "calls": calls,
              "source": source, "result": result, "error": error,
              "note": "Generated fixture; not official q or score."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    public = {k: v for k, v in report.items() if k not in ("source", "result")}
    public["calls"] = [{k: v for k, v in call.items() if k not in ("response", "input")} for call in calls]
    print(json.dumps(public,
                     ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", default="deepseek_v4")
    parser.add_argument("--deadline", type=float, default=100)
    parser.add_argument("--stations", type=int, choices=(8, 16), default=8)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--source-file", type=Path)
    inputs.add_argument("--review-from", type=Path)
    parser.add_argument("--output", type=Path, default=Path("dist/probes/latest.json"))
    asyncio.run(main(parser.parse_args()))
