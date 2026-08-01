#!/usr/bin/env python3
"""
Leonardo Bearer 自动获取（nodriver 版本，绕过 Cloudflare）

使用 nodriver（比 playwright-stealth 更隐蔽）完成：
  MS refresh token → Canva 邮箱验证码登录 → Leonardo SSO → 抓取 Bearer

使用示例：
  python3 scripts/leonardo_bootstrap_nodriver.py \\
    --account tests/canva_member_emails_xxx.txt \\
    --proxy http://127.0.0.1:10809 \\
    --out leonardo_session.json
"""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import nodriver as uc
except ImportError:
    raise SystemExit("缺 nodriver：pip install nodriver")

from core.mail_otp import get_otp

CANVA_LOGIN_URL = "https://www.canva.com/login"
LEONARDO_SIGNON_URL = "https://app.leonardo.ai/auth/canva/signon"

# Selectors
SEL_CANVA_EMAIL_METHOD = 'button[aria-label="Email"]'
SEL_CANVA_EMAIL_INPUT = 'input[type="email"]'
SEL_SUBMIT = 'button[type="submit"]'
SEL_CANVA_OTP_INPUT = 'input[autocomplete="one-time-code"]'


async def main_async(args):
    # 读取账号
    acct_file = Path(args.account)
    if not acct_file.exists():
        raise FileNotFoundError(f"账号文件不存在: {acct_file}")

    parts = acct_file.read_text().strip().split("|")
    if len(parts) < 4:
        raise ValueError("账号文件格式: email|field1|ms_refresh_token|client_id")

    acct = {
        "email": parts[0].strip(),
        "ms_refresh_token": parts[2].strip(),
        "client_id": parts[3].strip(),
    }

    print(f"[*] 账号邮箱域：{acct['email'].split('@')[1]}  client_id 前缀：{acct['client_id'][:12]}")

    # Bearer 捕获容器
    captured_bearer = {"value": None}

    async def capture_bearer(event):
        """拦截请求，捕获 Leonardo Bearer"""
        if "leonardo.ai" in event.request.url:
            headers = event.request.headers or {}
            auth = headers.get("Authorization") or headers.get("authorization")
            if auth and auth.startswith("Bearer "):
                bearer = auth[7:]
                if len(bearer) > 100 and not captured_bearer["value"]:
                    captured_bearer["value"] = bearer
                    print(f"[✓] 捕获 Leonardo Bearer (前30字符): {bearer[:30]}...")

    # 启动 nodriver
    browser_args = []
    if args.proxy:
        browser_args.append(f"--proxy-server={args.proxy}")

    browser = await uc.start(
        headless=not args.headful,
        browser_args=browser_args,
    )

    try:
        page = await browser.get(CANVA_LOGIN_URL)

        # 设置请求拦截
        page.add_handler(uc.cdp.network.RequestWillBeSent, capture_bearer)
        await page.send(uc.cdp.network.enable())

        print("[A] 打开 Canva 登录…")
        await asyncio.sleep(3)

        # 点击 Email 登录
        email_btn = await page.find(SEL_CANVA_EMAIL_METHOD, timeout=15)
        await email_btn.click()
        await asyncio.sleep(1)

        # 输入邮箱
        email_input = await page.find(SEL_CANVA_EMAIL_INPUT, timeout=10)
        await email_input.send_keys(acct["email"])

        # 提交
        submit_ts = time.time()
        submit_btn = await page.find(SEL_SUBMIT, timeout=10)
        await submit_btn.click()
        await asyncio.sleep(2)

        # 某些流程需要点两次
        try:
            submit_btn2 = await page.find(SEL_SUBMIT, timeout=4)
            await submit_btn2.click()
        except:
            pass

        print("[A] 等待 Canva 验证码邮件…")
        try:
            otp, _new_rt = get_otp(
                acct["ms_refresh_token"],
                acct["client_id"],
                after_ts=submit_ts,
                timeout=90,
            )
            print(f"[A] 收到验证码: {otp}")
        except Exception as exc:
            raise RuntimeError(f"获取 OTP 失败: {exc}")

        # 输入 OTP
        otp_input = await page.find(SEL_CANVA_OTP_INPUT, timeout=15)
        await otp_input.send_keys(otp)
        await asyncio.sleep(1)

        # 提交 OTP
        submit_otp_btn = await page.find(SEL_SUBMIT, timeout=10)
        await submit_otp_btn.click()

        print("[A] 提交验证码，等待登录完成…")
        await asyncio.sleep(5)

        # 跳转到 Leonardo SSO
        print("[B] 跳转 Leonardo SSO…")
        await page.get(LEONARDO_SIGNON_URL)
        await asyncio.sleep(5)

        # 等待 Bearer 捕获
        deadline = time.time() + 30
        while time.time() < deadline:
            if captured_bearer["value"]:
                break
            await asyncio.sleep(1)

        if not captured_bearer["value"]:
            raise RuntimeError("未捕获到 Leonardo Bearer")

        # 保存结果
        result = {
            "email": acct["email"],
            "bearer": captured_bearer["value"],
            "captured_at": time.time(),
        }

        if args.out:
            Path(args.out).write_text(json.dumps(result, indent=2))
            print(f"[✓] 已保存到 {args.out}")
        else:
            print(json.dumps(result, indent=2))

        return 0

    finally:
        browser.stop()


def main():
    parser = argparse.ArgumentParser(description="Leonardo Bearer 自动获取（nodriver 版本）")
    parser.add_argument("--account", required=True, help="账号文件路径（email|field1|ms_refresh_token|client_id）")
    parser.add_argument("--proxy", help="代理地址（例如 http://127.0.0.1:10809）")
    parser.add_argument("--out", help="输出 JSON 文件路径")
    parser.add_argument("--headful", action="store_true", help="显示浏览器窗口")

    args = parser.parse_args()

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
