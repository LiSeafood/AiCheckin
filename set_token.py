#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 accessToken 写进 config.json,并当场验证它能不能用。

用法(推荐,token 不出现在命令行历史里):
    python set_token.py
然后粘贴 token,回车。

也可以管道传入:
    echo <token> | python set_token.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("WORKBUDDY_DOMAIN", "www.codebuddy.cn")
STATUS_URL = f"https://{BASE}/v2/billing/meter/checkin-status"
CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def jwt_info(token: str) -> dict | None:
    if token.count(".") != 2:
        return None
    try:
        b64 = token.split(".")[1]
        b64 += "=" * (-len(b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(b64))
        exp = payload.get("exp")
        return {"exp": exp, "days_left": round((exp - time.time()) / 86400, 1) if exp else None}
    except Exception:
        return None


def probe(token: str) -> tuple[int, str]:
    """拿这个 token 去查一次签到状态,确认它真的能用。"""
    req = urllib.request.Request(
        STATUS_URL,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "ignore")[:300]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "ignore")[:300]
    except Exception as exc:
        return -1, str(exc)


def main() -> int:
    print("=" * 60)
    print("写入 WorkBuddy accessToken")
    print("=" * 60)
    print()
    print("请把 token 粘贴进来(就是 DevTools 里 Authorization: Bearer 后面那一串),")
    print("然后按回车:")
    print()

    if not sys.stdin.isatty():
        token = sys.stdin.read()
    else:
        token = input()

    # 清洗:去空白/换行/引号,去掉可能误粘的 Bearer 前缀
    token = "".join(token.split())
    token = token.strip('"').strip("'")
    if token.lower().startswith("bearer"):
        token = token[6:].strip()

    if not token:
        print("\n[失败] 没有读到 token。")
        return 1

    print()
    print(f"长度: {len(token)} 字符")
    if not token.startswith("eyJ"):
        print("[警告] 不像 JWT(正常应以 eyJ 开头),请确认没有复制错/截断。")
    info = jwt_info(token)
    if info and info.get("days_left") is not None:
        print(f"解析成功,剩余有效期: {info['days_left']} 天")
    else:
        print("[警告] 无法解析 JWT 的 exp —— 可能复制不完整。")

    print()
    print("正在用这个 token 试一次签到状态查询...")
    status, raw = probe(token)
    print(f"  HTTP {status}")
    print(f"  响应: {raw[:200]}")

    ok = status == 200
    if not ok:
        print()
        print("[失败] 这个 token 用不了,没有写入 config.json。")
        print("       常见原因:复制时被截断、粘了多余字符、已过期。")
        return 1

    # 保留 config.json 里已有的其它字段
    cfg = {}
    if os.path.isfile(CONFIG):
        try:
            cfg = json.load(open(CONFIG, encoding="utf-8"))
        except Exception:
            cfg = {}
    cfg["access_token"] = token
    cfg.setdefault("uid", "")

    with open(CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    print()
    print(f"[成功] 已写入 {CONFIG}")
    print()
    print("现在跑一次验证:")
    print("    python checkin.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
