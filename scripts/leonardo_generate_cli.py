#!/usr/bin/env python3
"""
Leonardo 出图 CLI —— 用一个现成的 Leonardo Bearer 直接验证 leonardo_client + leonardo_generation。

用途：绕开被 Cloudflare 挡住的自动登录，先端到端验证"拿到 Bearer 之后"的出图链路。
从你已登录 Leonardo 的浏览器里抓 Bearer：
  开发者工具 → Network → 任意一条 api.leonardo.ai 请求 → 请求头 Authorization: Bearer <这一长串>

⚠️ 从**住宅 IP / 你能正常访问 Leonardo 的环境**跑。数据中心 IP 会被 api.leonardo.ai 限流(429)。
⚠️ 本 CLI 会真实调用 api.leonardo.ai 并消耗账号额度。Bearer 是敏感凭据，勿提交/外发。

用法：
  python scripts/leonardo_generate_cli.py \
      --bearer "eyJ..." \
      --prompt "a cinematic photo of a red fox in snow" \
      --aspect 16:9 --n 1
  # 可选 --model-id <UUID>（默认 Nano Banana 2）、--size 1024x1024、--credits-only 只查额度
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.leonardo_client import LeonardoClient, LeonardoError  # noqa: E402
from core.leonardo_generation import generate_images  # noqa: E402

DEFAULT_MODEL_ID = "7418e71f-4133-4e1b-9895-bee19f48f2ce"  # Nano Banana 2


def main() -> int:
    ap = argparse.ArgumentParser(description="用现成 Bearer 验证 Leonardo 出图链路")
    ap.add_argument("--bearer", required=True, help="Leonardo Bearer JWT（浏览器 Network 里抓）")
    ap.add_argument("--prompt", default="a cinematic photo of a red fox in snow")
    ap.add_argument("--model-id", default=DEFAULT_MODEL_ID, help=f"默认 {DEFAULT_MODEL_ID}（Nano Banana 2）")
    ap.add_argument("--aspect", default="1:1", help="16:9 / 9:16 / 1:1 / 4:3")
    ap.add_argument("--size", default=None, help="如 1024x1024（与 --aspect 二选一，aspect 优先）")
    ap.add_argument("--n", type=int, default=1, help="张数 1..4")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--credits-only", action="store_true", help="只查额度，不出图")
    args = ap.parse_args()

    token = args.bearer.strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()

    client = LeonardoClient()

    # 1) 先查额度（顺带验证 Bearer 有效 + 协议层通）
    try:
        credits = client.get_credits(token)
        print(f"[额度] token balance = {credits}")
    except LeonardoError as exc:
        print(f"[!] 查额度失败（Bearer 可能失效/被拦）：{exc}")
        return 2
    except Exception as exc:  # 网络/风控
        print(f"[!] 查额度请求异常：{exc}")
        return 2

    if args.credits_only:
        return 0

    # 2) 出图
    try:
        result = generate_images(
            client,
            token,
            prompt=args.prompt,
            model_id=args.model_id,
            size=args.size,
            aspect_ratio=args.aspect,
            n=args.n,
            timeout=args.timeout,
        )
    except LeonardoError as exc:
        print(f"[!] 出图失败：{exc}")
        return 3
    except Exception as exc:
        print(f"[!] 出图请求异常：{exc}")
        return 3

    print("\n===== 出图成功 =====")
    print(f"  generation_id: {result['provider']['generation_id']}")
    print(f"  aspect_ratio : {result['provider']['aspect_ratio']}")
    for i, item in enumerate(result["data"], 1):
        print(f"  图 {i}: {item['url']}")
    print("\n完整响应(OpenAI 风格):")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
