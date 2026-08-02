#!/usr/bin/env python3
"""
Leonardo Bootstrap —— 验证性 SPIKE（非生产、非 CI）。

目的：端到端验证 spec §5 的登录链是否成立：
    微软邮箱 refresh token 读 OTP → Canva 邮箱验证码登录 → Canva→Leonardo SSO
    → 抓到 Leonardo 的 better-auth 会话 cookie（+ Bearer）。

这是「LeonardoBootstrapper」正式模块之前的探路脚本：selector 来自 create-canva-new 源码、
未经实跑；Canva/Leonardo 改版或风控都可能让它失败。跑通后再重构进 core/。

安全边界：
  - 只读账号文件里的 (client_id, refresh_token)，用 core.mail_otp 走 Graph 读 OTP。
  - 兑换会轮换微软 refresh token —— on_rotate 会把新 token 写回账号文件，否则账号被烧。
  - 抓到的 Leonardo cookie 只写本地文件，**绝不发送到任何外部服务器**。

环境：
  pip install playwright
  playwright install chromium
  ⚠️ 从**住宅 IP / 你手工验证成功的环境**跑。数据中心 IP 会被 Canva(403)/Leonardo(429) 风控拦截。

用法：
  python scripts/leonardo_bootstrap_spike.py \
      --account tests/canva_member_emails_1785423590111.txt \
      --out leonardo_session.json \
      --headful          # 首次调试建议带浏览器界面观察；稳定后去掉走 headless
"""
import argparse
import json
import sys
import time
from pathlib import Path

# 让脚本能 import 到项目的 core 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core.mail_otp import get_otp, MailOTPError  # noqa: E402

CANVA_LOGIN_URL = "https://www.canva.com/login"
LEONARDO_LOGIN_URL = "https://app.leonardo.ai/auth/login"

# create-canva-new 源码提取的 selector（spec §5，未实跑，失效时按真实页面调整）
SEL_CANVA_EMAIL_METHOD = 'button[aria-label="Email"]'
SEL_CANVA_EMAIL_INPUT = 'input[inputmode="email"]'
SEL_CANVA_OTP_INPUT = 'input[inputmode="numeric"]'
SEL_SUBMIT = 'button[type="submit"]'
SEL_CANVA_SSO_CANDIDATES = [
    'button:has-text("Continue with Canva")',
    'button:has-text("Canva")',
    "button.hw3gKA",
]
LEONARDO_SESSION_COOKIE = "__Secure-better-auth.session_token"


def load_account(path: str) -> dict:
    parts = [p.strip() for p in Path(path).read_text(encoding="utf-8").strip().split("|")]
    if len(parts) < 4:
        raise SystemExit(f"账号文件字段数不足（需要 邮箱|字段1|refresh_token|client_id）：{len(parts)}")
    return {
        "email": parts[0],
        "field1": parts[1],
        "refresh_token": parts[2],
        "client_id": parts[3],
        "_parts": parts,
    }


def persist_rotated(path: str, parts: list, new_rt: str) -> None:
    parts[2] = new_rt
    Path(path).write_text("|".join(parts), encoding="utf-8")
    print("  [mail] 微软 refresh token 已轮换并写回账号文件（旧的已失效）")


def _capture_bearer(request, sink: dict) -> None:
    try:
        auth = request.headers.get("authorization", "")
    except Exception:
        return
    if auth.startswith("Bearer ") and "api.leonardo.ai" in request.url:
        tok = auth[len("Bearer "):]
        if len(tok) > 50 and (not sink.get("bearer") or len(tok) > len(sink["bearer"])):
            sink["bearer"] = tok
            print(f"  [leo] 抓到 Bearer（{len(tok)} 字符）")


def _click_canva_sso(page) -> bool:
    for sel in SEL_CANVA_SSO_CANDIDATES:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=6000)
            loc.click()
            print(f"  [leo] 点了 Canva SSO 按钮：{sel}")
            return True
        except Exception:
            continue
    return False


def run(args) -> int:
    acct = load_account(args.account)
    print(f"[*] 账号邮箱域：{acct['email'].split('@')[-1]}  client_id 前缀：{acct['client_id'][:8]}")

    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError:
        raise SystemExit("缺 playwright 或 playwright_stealth：pip install playwright playwright-stealth && playwright install chromium")

    sink = {"bearer": None, "session_cookie": None, "all_leonardo_cookies": []}

    with sync_playwright() as p:
        launch_opts = {
            "headless": not args.headful,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--lang=en-US",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
        }
        if args.proxy:
            launch_opts["proxy"] = {"server": args.proxy}
        browser = p.chromium.launch(**launch_opts)
        ctx = browser.new_context(
            locale="en-US",
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        )
        # 注入 JS 隐藏 webdriver 特征
        ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.navigator.chrome = {runtime: {}};
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
        """)
        page = ctx.new_page()
        Stealth().apply_stealth_sync(page)  # 应用 stealth 反检测
        page.on("request", lambda r: _capture_bearer(r, sink))

        # ---- A. Canva 邮箱验证码登录 ----
        print("[A] 打开 Canva 登录…")
        page.goto(CANVA_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        page.click(SEL_CANVA_EMAIL_METHOD, timeout=15000)
        page.fill(SEL_CANVA_EMAIL_INPUT, acct["email"])
        submit_ts = time.time()  # OTP 必须晚于此刻，避免读到旧验证码
        page.click(SEL_SUBMIT, timeout=10000)
        try:
            page.click(SEL_SUBMIT, timeout=4000)  # 某些流程要点两次
        except Exception:
            pass

        # ---- 用 MailOTPReader 读这次触发的新验证码 ----
        print("[A] 等 Canva 验证码邮件…")
        try:
            otp, _new_rt = get_otp(
                acct["client_id"],
                acct["refresh_token"],
                since_ts=submit_ts,
                on_rotate=lambda rt: persist_rotated(args.account, acct["_parts"], rt),
                poll_interval=5,
                timeout=150,
            )
        except MailOTPError as exc:
            print(f"[!] 读 OTP 失败：{exc}")
            ctx.close(); browser.close()
            return 2
        print(f"[A] 收到验证码：{otp}")
        page.fill(SEL_CANVA_OTP_INPUT, otp, timeout=15000)
        page.click(SEL_SUBMIT, timeout=10000)
        page.wait_for_load_state("networkidle", timeout=30000)
        print("[A] Canva 会话应已建立")

        # ---- B. Canva → Leonardo SSO ----
        print("[B] 打开 Leonardo 登录，走 Canva SSO…")
        page.goto(LEONARDO_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        if not _click_canva_sso(page):
            print("[!] 没找到 Canva SSO 按钮 —— 页面可能已改版，需重新抓 selector")
        page.wait_for_load_state("networkidle", timeout=45000)
        # 导航几个页面触发 api.leonardo.ai 调用，便于抓 Bearer
        for u in ["https://app.leonardo.ai/", "https://app.leonardo.ai/image-generation"]:
            try:
                page.goto(u, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
            except Exception:
                pass

        # ---- 抓 cookie ----
        for c in ctx.cookies():
            dom = (c.get("domain") or "").lower()
            if "leonardo" in dom:
                sink["all_leonardo_cookies"].append({"name": c.get("name"), "domain": dom})
                if c.get("name") == LEONARDO_SESSION_COOKIE or "session_token" in (c.get("name") or "").lower():
                    sink["session_cookie"] = c

        ctx.close()
        browser.close()

    ok = bool(sink["session_cookie"] or sink["bearer"])
    print("\n===== SPIKE 结果 =====")
    print(f"  better-auth session cookie: {'✅ 抓到' if sink['session_cookie'] else '❌ 未抓到'}")
    print(f"  Leonardo Bearer: {'✅ 抓到' if sink['bearer'] else '❌ 未抓到'}")
    print(f"  leonardo 域 cookie 总数: {len(sink['all_leonardo_cookies'])}")
    if sink["all_leonardo_cookies"]:
        print("  cookie 名清单：", [c["name"] for c in sink["all_leonardo_cookies"]])

    if ok:
        out = Path(args.out)
        out.write_text(json.dumps({
            "session_cookie": sink["session_cookie"],
            "bearer": sink["bearer"],
            "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n💾 会话已保存到本地：{out}（含真实凭据，勿提交 git、勿外发）")
    else:
        print("\n[!] 没抓到会话。排查：① 是否被风控挡在 Canva 登录/验证码页 ②"
              " selector 是否失效 ③ 是否真从住宅 IP 跑。首次建议加 --headful 观察。")
    return 0 if ok else 3


def main() -> int:
    ap = argparse.ArgumentParser(description="Leonardo bootstrap 验证 spike")
    ap.add_argument("--account", required=True, help="账号文件路径（邮箱|字段1|refresh_token|client_id）")
    ap.add_argument("--out", default="leonardo_session.json", help="抓到的会话输出文件")
    ap.add_argument("--headful", action="store_true", help="带浏览器界面（首次调试用）")
    ap.add_argument("--proxy", help="HTTP/HTTPS 代理地址（如 http://127.0.0.1:10809）")
    return run(ap.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
