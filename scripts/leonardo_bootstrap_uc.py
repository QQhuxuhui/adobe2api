#!/usr/bin/env python3
"""
Leonardo Bearer 自动获取（undetected-chromedriver 版本）

使用 undetected-chromedriver 绕过 Cloudflare Turnstile：
  MS refresh token → Canva 邮箱验证码登录 → Leonardo SSO → 抓取 Bearer

使用示例：
  python3 scripts/leonardo_bootstrap_uc.py \\
    --account tests/canva_member_emails_xxx.txt \\
    --proxy http://127.0.0.1:10809 \\
    --out leonardo_session.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import undetected_chromedriver as uc
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    raise SystemExit("缺依赖：pip install undetected-chromedriver")

from core.mail_otp import get_otp

CANVA_LOGIN_URL = "https://www.canva.com/login"
LEONARDO_SIGNON_URL = "https://app.leonardo.ai/auth/canva/signon"

# Selectors
SEL_CANVA_EMAIL_METHOD = 'button[aria-label="Email"]'
SEL_CANVA_EMAIL_INPUT = 'input[type="email"]'
SEL_SUBMIT = 'button[type="submit"]'
SEL_CANVA_OTP_INPUT = 'input[autocomplete="one-time-code"]'


def main():
    parser = argparse.ArgumentParser(description="Leonardo Bearer 自动获取（UC 版本）")
    parser.add_argument("--account", required=True, help="账号文件路径")
    parser.add_argument("--proxy", help="代理地址")
    parser.add_argument("--out", help="输出 JSON 文件路径")
    parser.add_argument("--headful", action="store_true", help="显示浏览器")

    args = parser.parse_args()

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

    print(f"[*] 账号：{acct['email']}")

    # Bearer 捕获容器
    captured_bearer = {"value": None}

    # 配置 Chrome
    options = uc.ChromeOptions()
    if not args.headful:
        options.add_argument("--headless=new")
    if args.proxy:
        options.add_argument(f"--proxy-server={args.proxy}")

    # 启动浏览器（指定 Chrome 140 版本）
    driver = uc.Chrome(options=options, version_main=140, use_subprocess=True)

    try:
        # 设置请求拦截（捕获 Bearer）
        driver.execute_cdp_cmd("Network.enable", {})

        def intercept_request(message):
            if message.get("method") == "Network.requestWillBeSent":
                params = message.get("params", {})
                request = params.get("request", {})
                url = request.get("url", "")
                headers = request.get("headers", {})

                if "leonardo.ai" in url:
                    auth = headers.get("Authorization") or headers.get("authorization")
                    if auth and auth.startswith("Bearer ") and not captured_bearer["value"]:
                        bearer = auth[7:]
                        if len(bearer) > 100:
                            captured_bearer["value"] = bearer
                            print(f"[✓] 捕获 Bearer (前30字符): {bearer[:30]}...")

        driver.execute_cdp_cmd("Network.setRequestInterception", {"patterns": [{"urlPattern": "*"}]})

        print("[A] 打开 Canva 登录…")
        driver.get(CANVA_LOGIN_URL)
        wait = WebDriverWait(driver, 15)

        # 等待页面加载
        time.sleep(3)

        # 点击 Email 登录
        email_btn = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_CANVA_EMAIL_METHOD)))
        email_btn.click()
        time.sleep(1)

        # 输入邮箱
        email_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_CANVA_EMAIL_INPUT)))
        email_input.send_keys(acct["email"])

        # 提交
        submit_ts = time.time()
        submit_btn = driver.find_element(By.CSS_SELECTOR, SEL_SUBMIT)
        submit_btn.click()
        time.sleep(2)

        # 某些流程需要点两次
        try:
            submit_btn2 = WebDriverWait(driver, 4).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, SEL_SUBMIT))
            )
            submit_btn2.click()
        except:
            pass

        print("[A] 等待验证码邮件…")
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
        otp_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEL_CANVA_OTP_INPUT)))
        otp_input.send_keys(otp)
        time.sleep(1)

        # 提交 OTP
        submit_otp_btn = driver.find_element(By.CSS_SELECTOR, SEL_SUBMIT)
        submit_otp_btn.click()

        print("[A] 提交验证码，等待登录完成…")
        time.sleep(5)

        # 跳转到 Leonardo SSO
        print("[B] 跳转 Leonardo SSO…")
        driver.get(LEONARDO_SIGNON_URL)
        time.sleep(5)

        # 监听网络请求（简化版：直接从 localStorage/cookies 获取）
        # 尝试执行 JS 获取 Bearer
        time.sleep(3)

        # 如果还没捕获到，尝试刷新页面触发请求
        if not captured_bearer["value"]:
            print("[B] 刷新页面触发请求…")
            driver.refresh()
            time.sleep(5)

        if not captured_bearer["value"]:
            raise RuntimeError("未捕获到 Leonardo Bearer（尝试手动检查浏览器）")

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
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
