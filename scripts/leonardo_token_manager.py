#!/usr/bin/env python3
"""
Leonardo Token 管理工具
用途：添加/更新/查看 Leonardo Bearer token，持久化到 config/tokens.json

使用示例：
  # 查看当前 leonardo tokens
  python3 scripts/leonardo_token_manager.py --list

  # 添加新 Bearer（自动标记 type=leonardo）
  python3 scripts/leonardo_token_manager.py --add "eyJraWQi..."

  # 更新现有 token 的 Bearer
  python3 scripts/leonardo_token_manager.py --update a062ccee "eyJraWQi..."

  # 重置 token 状态为 active
  python3 scripts/leonardo_token_manager.py --reset a062ccee
"""
import sys
import argparse
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.token_mgr import token_manager


def list_tokens():
    """列出所有 leonardo tokens"""
    tokens = [t for t in token_manager.tokens if t.get("type") == "leonardo"]
    if not tokens:
        print("没有 leonardo token")
        return

    print(f"共 {len(tokens)} 个 leonardo token:\n")
    for t in tokens:
        print(f"  ID: {t['id']}")
        print(f"  Type: {t.get('type')}")
        print(f"  Status: {t.get('status')}")
        print(f"  Fails: {t.get('fails', 0)}")
        print(f"  Account ID: {t.get('account_id', 'N/A')}")
        print(f"  Bearer 前30字符: {t['value'][:30]}...")
        print()


def add_token(bearer: str):
    """添加新的 Leonardo Bearer"""
    result = token_manager.add(bearer)

    # 如果自动检测未标记 type，手动标记
    if not result.get("type"):
        with token_manager._lock:
            for t in token_manager.tokens:
                if t["id"] == result["id"]:
                    t["type"] = "leonardo"
                    break
            token_manager.save()
        result["type"] = "leonardo"

    print(f"✅ Token 已添加:")
    print(f"  ID: {result['id']}")
    print(f"  Type: {result.get('type')}")
    print(f"  Status: {result['status']}")
    print(f"\n已保存到 config/tokens.json")


def update_token(token_id: str, bearer: str):
    """更新现有 token 的 Bearer 值"""
    with token_manager._lock:
        found = False
        for t in token_manager.tokens:
            if t["id"] == token_id:
                t["value"] = bearer
                t["status"] = "active"
                t["fails"] = 0
                t["error_until"] = 0
                if not t.get("type"):
                    t["type"] = "leonardo"
                found = True
                print(f"✅ Token {token_id} 已更新:")
                print(f"  Type: {t.get('type')}")
                print(f"  Status: {t['status']}")
                break

        if not found:
            print(f"❌ 未找到 token_id={token_id}")
            return

        token_manager.save()
    print(f"\n已保存到 config/tokens.json")


def reset_token(token_id: str):
    """重置 token 状态为 active"""
    with token_manager._lock:
        found = False
        for t in token_manager.tokens:
            if t["id"] == token_id:
                t["status"] = "active"
                t["fails"] = 0
                t["error_until"] = 0
                found = True
                print(f"✅ Token {token_id} 状态已重置为 active")
                break

        if not found:
            print(f"❌ 未找到 token_id={token_id}")
            return

        token_manager.save()
    print(f"\n已保存到 config/tokens.json")


def main():
    parser = argparse.ArgumentParser(description="Leonardo Token 管理工具")
    parser.add_argument("--list", action="store_true", help="列出所有 leonardo tokens")
    parser.add_argument("--add", metavar="BEARER", help="添加新的 Leonardo Bearer")
    parser.add_argument("--update", nargs=2, metavar=("TOKEN_ID", "BEARER"),
                        help="更新现有 token 的 Bearer")
    parser.add_argument("--reset", metavar="TOKEN_ID", help="重置 token 状态为 active")

    args = parser.parse_args()

    if args.list:
        list_tokens()
    elif args.add:
        add_token(args.add)
    elif args.update:
        update_token(args.update[0], args.update[1])
    elif args.reset:
        reset_token(args.reset)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
