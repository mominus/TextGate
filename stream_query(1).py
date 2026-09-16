#!/usr/bin/env python3
"""StackAI 流式推理调用测试脚本（JSON Lines，非标准 SSE）。

对应官方非流式示例的流式版本：

    非流式  POST /inference/v0/run/{org}/{flow}
    流式    POST /inference/v0/stream/{org}/{flow}   body 必须含 "stream": true

实测契约（docs/research/03-stream-protocol-actual.md）：
1. 流式响应**不是标准 SSE**，而是 JSON Lines：每行一个完整 JSON 对象，用
   ``\\n`` 分隔；偶发的 ``data: `` 前缀需剥离。
2. ``outputs["out-0"]`` 是 **token 级增量**（非全量），客户端必须累加。
3. 终止信号是 ``metadata.done == true``（或 ``state == "COMPLETED"`` 兜底）。
4. 首 token 前有一串 ``progress_data`` 进度帧（前置节点约 1.3–1.5s）。
5. 必须显式传 ``"stream": true``，否则 outputs 恒为空（V2 实测）。
6. 错误体为 ``{"detail": "<message>"}``（401/404）。

依赖：requests（``pip install requests``）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

import requests

# 实测可用主机是 api.stack-ai.com（带连字符）。官方示例常写作 api.stackai.com，
# 但本机实测 stack-inference.com TLS 失败、api.stack-ai.com 正常 —— 见 §0。
DEFAULT_BASE_URL = "https://api.stack-ai.com"

# 流式端点模板（与 /run/ 对应的 stream 路径）。
STREAM_PATH = "/inference/v0/stream/{org}/{flow}"

DEFAULT_MODEL = "gemini-3.1-flash-lite"
DEFAULT_LANG = "Simplified Chinese"

# 建连超时 / 两次读取之间的超时（长时间生成给足余量；None 表示读不超时）。
CONNECT_TIMEOUT = 15.0
READ_TIMEOUT = 300.0


def parse_line(line: str) -> dict[str, Any] | None:
    """把一行解析成 JSON 对象；剥离偶发的 ``data: `` 前缀与空行。"""
    text = line.strip()
    if not text:
        return None
    # 容错：实测有 1 帧带 ``data: `` 前缀（见报告 §1.2）
    for _ in range(4):
        if text.lower().startswith("data:"):
            text = text[len("data:"):].lstrip()
            continue
        break
    if not text or text.startswith(":"):
        return None  # SSE 注释/心跳行
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        print(f"[跳过] 无法解析的帧：{text[:120]!r}", file=sys.stderr)
        return None
    return data if isinstance(data, dict) else None


def is_done(frame: dict[str, Any]) -> bool:
    """流结束判定：``metadata.done == true`` 为主，``state == COMPLETED`` 兜底。"""
    md = frame.get("metadata") or {}
    done = md.get("done")
    if done is True or str(done).lower() == "true":
        return True
    return frame.get("state") == "COMPLETED"


def extract_detail(resp: requests.Response, body: str) -> str:
    """从错误响应体提取可读信息（``{"detail":...}`` 或原始文本/HTML）。"""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])
    except json.JSONDecodeError:
        pass
    return body[:300] or "(空响应体)"


def stream_query(
    base_url: str,
    org_id: str,
    flow_id: str,
    public_key: str,
    payload: dict[str, Any],
    connect_timeout: float,
    read_timeout: float | None,
    debug: bool = False,
) -> int:
    """发起流式调用，逐帧消费并实时打印。返回进程退出码（0=成功）。"""
    url = base_url.rstrip("/") + STREAM_PATH.format(org=org_id, flow=flow_id)
    headers = {
        "Authorization": f"Bearer {public_key}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    print(f"POST {url}")
    print(f"payload: {json.dumps(payload, ensure_ascii=False)}\n")

    try:
        with requests.post(
            url,
            headers=headers,
            json=payload,
            stream=True,
            timeout=(connect_timeout, read_timeout),
        ) as resp:
            if resp.status_code != 200:
                body = resp.text
                print(
                    f"[错误] HTTP {resp.status_code}: {extract_detail(resp, body)}",
                    file=sys.stderr,
                )
                return 1

            # iter_lines 内部按 \n 切分并缓冲跨 chunk 的残行，天然适配 JSON Lines。
            parts: list[str] = []
            saw_content = False
            last_node: str | None = None
            stats = {"progress": 0, "content": 0, "done": 0}
            seen_output_keys: set[str] = set()
            last_state: str | None = None
            final_outputs: Any = None

            for line in resp.iter_lines(decode_unicode=True):
                raw = line or ""
                frame = parse_line(raw)
                if frame is None:
                    if debug and raw.strip():
                        print(f"[原始非JSON] {raw[:200]}", file=sys.stderr)
                    continue
                if debug:
                    print(f"[帧] {json.dumps(frame, ensure_ascii=False)}", file=sys.stderr)

                # 流内错误帧（最优先）
                if frame.get("error"):
                    print(f"\n[流内错误] {frame['error']}", file=sys.stderr)
                    return 1

                outputs = frame.get("outputs")
                if isinstance(outputs, dict):
                    seen_output_keys.update(outputs.keys())

                # ⚠️ 关键：内容帧可能**同时带 progress_data**（实测帧如此），
                # 必须优先判 out-0，否则带 progress_data 的内容帧会被误判为纯进度帧跳过。
                out = outputs.get("out-0") if isinstance(outputs, dict) else None
                if isinstance(out, str) and out:
                    if not saw_content:
                        print("\r" + " " * 60, end="", flush=True)  # 清掉进度行
                        print("\r", end="", flush=True)
                        saw_content = True
                    print(out, end="", flush=True)
                    parts.append(out)
                    stats["content"] += 1
                else:
                    # 纯进度帧：仅在 out-0 为空时（去重后打印当前节点）
                    prog = frame.get("progress_data")
                    if prog:
                        stats["progress"] += 1
                        node = prog.get("current_node")
                        if node and node != last_node:
                            last_node = node
                            print(f"\r[进度] {node} ...", end="", flush=True)

                if is_done(frame):
                    stats["done"] += 1
                    last_state = frame.get("state")
                    final_outputs = outputs
                    break

            if not saw_content:
                print("\r" + " " * 60, end="", flush=True)
            print("\n")
            print(f"[完成] 累计输出字符数 = {sum(len(p) for p in parts)}")
            print(
                f"[统计] 进度帧={stats['progress']} 内容帧={stats['content']} "
                f"完成帧={stats['done']} state={last_state}"
            )
            if not saw_content:
                print("[诊断] 未收到内容帧（outputs.out-0）。")
                print(f"[诊断] 上游 outputs 出现过的 key：{sorted(seen_output_keys) or '(无)'}")
                if final_outputs:
                    print(f"[诊断] 终止帧 outputs：{json.dumps(final_outputs, ensure_ascii=False)}")
                print("[诊断] 请确认走的是流式端点 /stream/ 且 body 含 stream:true；"
                      "可加 --debug 查看原始帧，或用 /run/ 非流式对照。")
            return 0

    except requests.exceptions.ReadTimeout:
        print("\n[错误] 读取超时（可用 --read-timeout 调大）", file=sys.stderr)
        return 1
    except requests.exceptions.ConnectTimeout:
        print("[错误] 连接超时", file=sys.stderr)
        return 1
    except requests.exceptions.ConnectionError as exc:
        print(f"[错误] 连接失败：{exc}", file=sys.stderr)
        return 1
    except requests.exceptions.RequestException as exc:
        print(f"[错误] 请求异常：{exc}", file=sys.stderr)
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="StackAI 流式推理调用（/inference/v0/stream/{org}/{flow}）"
    )
    parser.add_argument("--base-url", default=os.environ.get("STACKAI_BASE_URL", DEFAULT_BASE_URL))
    parser.add_argument("--org", default=os.environ.get("STACKAI_ORG_ID"), help="org_id")
    parser.add_argument("--flow", default=os.environ.get("STACKAI_FLOW_ID"), help="flow_id")
    parser.add_argument(
        "--key", default=os.environ.get("STACKAI_PUBLIC_KEY"), help="public_api_key"
    )
    parser.add_argument("--prompt", default="Hello, world!", help="in-0")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="in-1（路由提示，非严格校验）")
    parser.add_argument("--lang", default=DEFAULT_LANG, help="in-2")
    parser.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT)
    parser.add_argument(
        "--read-timeout", type=float, default=READ_TIMEOUT, help="两次读之间的超时，None 用 0 表示"
    )
    parser.add_argument("--debug", action="store_true", help="打印每帧原始 JSON 到 stderr")
    args = parser.parse_args(argv)

    if not args.org or not args.flow or not args.key:
        parser.error("缺少 org/flow/key：用 --org/--flow/--key 或环境变量 STACKAI_ORG_ID/"
                     "STACKAI_FLOW_ID/STACKAI_PUBLIC_KEY 提供")

    payload = {
        "in-0": args.prompt,
        "in-1": args.model,
        "in-2": args.lang,
        "user_id": "",
        "stream": True,  # 必须显式 true，否则 outputs 恒为空
    }
    read_timeout = None if args.read_timeout <= 0 else args.read_timeout

    return stream_query(
        args.base_url,
        args.org,
        args.flow,
        args.key,
        payload,
        args.connect_timeout,
        read_timeout,
        debug=args.debug,
    )


if __name__ == "__main__":
    sys.exit(main())
