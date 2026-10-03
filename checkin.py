#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日自动签到 — WorkBuddy + Trae CN + Qoder(非官方,接口逆向自各客户端)

设计目标:
  * 纯 Python 标准库,零第三方依赖
  * 本地运行:直接从本机客户端数据读取登录态,零配置
  * 多账号:客户端登录态自动截留入库(accounts.local.json),逐账号签到
  * 自动续期:Trae / Qoder 的 refreshToken 轮换全自动处理,不依赖打开客户端
  * 云端运行:从环境变量(Secrets)读取 token,不落盘
  * 幂等:已签到判定为成功,不报错
  * 容错:网络波动 / 服务端限流时自动重试

用法:
  本地:  python checkin.py            # WorkBuddy + Trae + Qoder 都签
  跳过:  python checkin.py --no-trae  # 跳过 Trae
         python checkin.py --no-qoder # 跳过 Qoder
  导出:  python checkin.py --export   # 输出 token,用于更新 GitHub Secrets
  帮助:  python checkin.py --help
  云端:  使用 workflow env 注入 WORKBUDDY_ACCESS_TOKEN / WORKBUDDY_UID

  日志:  默认写入脚本同级 logs/checkin.log(超过 512KB 自动轮转)。
         任务计划程序等无窗口场景依赖此文件查看结果。
         用 CHECKIN_LOG_FILE 可自定义路径,设为空串关闭。

Token 保活 / 到期怎么办(重要):
  * WorkBuddy: 桌面客户端使用时会自动刷新 accessToken 并写回本地
    workbuddy-desktop.info。本地模式总是读到最新 token,无需自己刷新
    (刷新接口需要 daemon 里的 client 凭证,外部不可用)。脚本每次运行
    都会打印剩余天数,不足 7 天时醒目告警 —— 届时打开一次客户端即可。
    云端模式(Secrets)token 不自动续期,到期前需重新 --export。
  * Trae: 自动续期 —— token 剩余不足 24h 时,用本地保存的 refreshToken
    调 ExchangeToken 换新 JWT(参考 trae-mate 的发现: SOLO ClientID +
    极简参数即可,无需设备指纹签名),并把新 token / 轮换后的
    refreshToken 重新加密写回 storage.json。
  * Qoder: 自动续期 —— token 剩余不足 72h 时调 deviceToken/refresh
    换新并写回 auth.v1.dat;refreshToken 有效期约 1 年。
  * WorkBuddy 本地凭据为密文,解密密钥需先运行一次 extract_wbkey.mjs
    提取(结果写入 config.json 的 wb_at_rest_key,脚本按需读取)。

关于 Trae 签到头(参考 trae-mate 项目):
  领取接口需要完整的客户端伪装请求头(伪设备身份),否则容易命中
  9074 限流。本版按 trae-mate 的算法,按 user_id 确定性派生一套稳定的
  伪设备标识(x-device-id / x-market-user-id / vscode-sessionid),
  已通过其测试向量验证。
"""

from __future__ import annotations

import base64
import calendar
import glob
import hashlib
import json
import os
import platform
import random
import re
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

WORKBUDDY_BASE = os.environ.get("WORKBUDDY_DOMAIN", "www.codebuddy.cn")
WORKBUDDY_STATUS_URL = f"https://{WORKBUDDY_BASE}/v2/billing/meter/checkin-status"
WORKBUDDY_CLAIM_URL = f"https://{WORKBUDDY_BASE}/v2/billing/meter/daily-checkin"

TRAE_BASE = "https://api.trae.cn"
TRAE_STATUS_URL = f"{TRAE_BASE}/trae/api/v2/ug/checkin_credits/status"
TRAE_CLAIM_URL = f"{TRAE_BASE}/trae/api/v2/ug/checkin_credits/claim"
TRAE_CREDITS_URL = f"{TRAE_BASE}/trae/api/v2/pay/ide_user_ent_usage"
# 刷新端点(注意是 api.trae.com.cn,与签到域不同;参考 trae-mate jwt.rs)
TRAE_EXCHANGE_URL = "https://api.trae.com.cn/cloudide/api/v3/trae/oauth/ExchangeToken"
TRAE_CLIENT_ID = "en1oxy7wnw8j9n"  # SOLO / SOLO Lite 端的 ClientID

# WorkBuddy 桌面端登录态文件
WORKBUDDY_AUTH_REL = os.path.join(
    "CodeBuddyExtension", "Data", "Public", "auth", "workbuddy-desktop.info"
)

# Trae CN 登录态文件
TRAE_STORAGE_REL = os.path.join("Trae CN", "User", "globalStorage", "storage.json")
TRAE_SOLO_STORAGE_REL = os.path.join("TRAE SOLO CN", "User", "globalStorage", "storage.json")
TRAE_KEY = "iCubeAuthInfo://icube.cloudide"

# Trae 领取接口常返回 9074(参与用户太多)限流,实测约 2~4 分钟恢复。
# 默认 14 次重试:前 4 轮约 25~45s,之后 60~90s,总计可覆盖约 12 分钟。
MAX_RETRY = int(os.environ.get("CHECKIN_MAX_RETRY", "14"))
RETRY_WAIT_MIN = int(os.environ.get("CHECKIN_RETRY_WAIT_MIN", "15"))
RETRY_WAIT_MAX = int(os.environ.get("CHECKIN_RETRY_WAIT_MAX", "30"))
HTTP_TIMEOUT = int(os.environ.get("CHECKIN_HTTP_TIMEOUT", "30"))


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

# 日志文件(供任务计划程序等无窗口场景使用)。可用 CHECKIN_LOG_FILE 覆盖。
# 默认写在脚本同级 logs/checkin.log;设为空字符串 "0" 可关闭。
_DEFAULT_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "checkin.log")
_env_log = os.environ.get("CHECKIN_LOG_FILE")
LOG_FILE: str | None = _DEFAULT_LOG if _env_log is None else (_env_log or None)
LOG_MAX_BYTES = int(os.environ.get("CHECKIN_LOG_MAX_BYTES", str(512 * 1024)))
LOG_RETENTION_DAYS = int(os.environ.get("CHECKIN_LOG_RETENTION_DAYS", "30"))


def _trim_log() -> None:
    """日志只保留最近 N 天(默认 30),每次启动时清理一次(含轮转备份)。"""
    if not LOG_FILE:
        return
    try:
        files = [LOG_FILE]
        if os.path.isfile(LOG_FILE + ".1"):
            files.append(LOG_FILE + ".1")
        cutoff = time.strftime("%Y-%m-%d",
                               time.localtime(time.time() - LOG_RETENTION_DAYS * 86400))
        kept = []
        total = 0
        for f in files:
            for line in open(f, encoding="utf-8", errors="ignore"):
                total += 1
                m = re.match(r"\[(\d{4}-\d\d-\d\d) ", line)
                if m and m.group(1) >= cutoff:
                    kept.append(line)
        if len(kept) >= total:
            return
        tmp = LOG_FILE + ".trim"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
        os.replace(tmp, LOG_FILE)
        if os.path.isfile(LOG_FILE + ".1"):
            os.remove(LOG_FILE + ".1")
    except Exception:
        pass


def _rotate_log(path: str) -> None:
    """日志超过上限时轮转一次(.1 备份),避免无限增长。"""
    try:
        if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            bak = path + ".1"
            if os.path.exists(bak):
                os.remove(bak)
            os.replace(path, bak)
    except Exception:
        pass


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    if LOG_FILE:
        try:
            _rotate_log(LOG_FILE)
            parent = os.path.dirname(os.path.abspath(LOG_FILE))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(LOG_FILE, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass


def mask(token) -> str:
    """只显示 token 头部,绝不打印完整 token。

    兼容非字符串输入 —— WorkBuddy 5.6.2 起本地 token 变成加密对象
    ({"$wbEncrypted":1,"envelope":"..."}),若不做防护会抛出
    KeyError: slice(None, 12, None) 这种看不懂的错误。
    """
    if token is None:
        return "(empty)"
    if not isinstance(token, str):
        return f"(非字符串: {type(token).__name__})"
    if not token:
        return "(empty)"
    return f"{token[:12]}...({len(token)} chars)"


# WorkBuddy 本地凭据加密(at-rest encryption)的标记
WB_ENCRYPTED_MARKER = "$wbEncrypted"

# WorkBuddy 5.6.2 起本地凭据为 AES-256-GCM 密文,解密密钥是客户端编译期内嵌的
# 静态密钥(所有安装相同,非用户数据)。出于发布合规考虑,本脚本**不内置**该
# 密钥 —— 用户运行一次 `node extract_wbkey.mjs` 即可提取并写入 config.json 的
# "wb_at_rest_key" 字段;也可用环境变量 WORKBUDDY_AT_REST_KEY 提供。
# 提取方法见 extract_wbkey.mjs。若未来版本更换了密钥,字段解密时 keyId 校验会
# 失败并给出明确提示 —— 重新运行提取脚本、更新 config.json 即可。

_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def _read_config() -> dict:
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _load_wb_at_rest_key() -> str | None:
    env = (os.environ.get("WORKBUDDY_AT_REST_KEY") or "").strip()
    if env:
        return env
    return (_read_config().get("wb_at_rest_key") or "").strip() or None


def is_encrypted_field(value) -> bool:
    """判断是否是 WorkBuddy 的加密字段包装。

    形态: {"$wbEncrypted": 1, "envelope": "<base64>"}
    envelope 内是 AES-256-GCM 套件 1:
        {"suite":1,"keyId":"<16 hex>","nonce":"","authTag":"","ciphertext":""}
    密钥来自 config.json 的 wb_at_rest_key(由 extract_wbkey.mjs 提取),
    派生: key = SHA256(密钥 base64 字符串的 UTF-8 字节)。
    脚本可以离线解密,见 wb_unseal_field()。
    """
    return isinstance(value, dict) and value.get(WB_ENCRYPTED_MARKER) == 1


# --------------------------------------------------------------------------
# WorkBuddy at-rest 字段解密(纯 Python AES-256-GCM,零依赖)
#
# 逆向自 WorkBuddy 5.6.2 app.asar 内 packages/at-rest-crypto:
#   * 密钥: AES key = SHA256(atRestSecretKey 的 base64 字符串按 UTF-8 字节)
#           keyId  = SHA256(AES key).hex()[:16]   (envelope 里的 keyId)
#   * 封套: {"suite":1,"keyId":..., "nonce":b64(12B), "authTag":b64(16B),
#            "ciphertext":b64}
#   * AAD  (sym-v1): "WB-AAD\0" || 0x01 || LP(framing魔法值) || LP("sym-v1")
#                    || u32be(suite) || LP(keyId) || framing序号
#                    || 0x00(无sequence) || 0x00(无final)
#     其中 LP(x) = u32be(len(x)) || x;
#     framing: file -> "WBEF1"/序号1, field -> "WBEV1"/序号2。
#     登录态文件里的字段全部用 framing="field"。
# 实现已与 Node OpenSSL 的 aes-256-gcm 交叉验证一致。
# --------------------------------------------------------------------------

_AES_SBOX = [
    0x63,0x7C,0x77,0x7B,0xF2,0x6B,0x6F,0xC5,0x30,0x01,0x67,0x2B,0xFE,0xD7,0xAB,0x76,
    0xCA,0x82,0xC9,0x7D,0xFA,0x59,0x47,0xF0,0xAD,0xD4,0xA2,0xAF,0x9C,0xA4,0x72,0xC0,
    0xB7,0xFD,0x93,0x26,0x36,0x3F,0xF7,0xCC,0x34,0xA5,0xE5,0xF1,0x71,0xD8,0x31,0x15,
    0x04,0xC7,0x23,0xC3,0x18,0x96,0x05,0x9A,0x07,0x12,0x80,0xE2,0xEB,0x27,0xB2,0x75,
    0x09,0x83,0x2C,0x1A,0x1B,0x6E,0x5A,0xA0,0x52,0x3B,0xD6,0xB3,0x29,0xE3,0x2F,0x84,
    0x53,0xD1,0x00,0xED,0x20,0xFC,0xB1,0x5B,0x6A,0xCB,0xBE,0x39,0x4A,0x4C,0x58,0xCF,
    0xD0,0xEF,0xAA,0xFB,0x43,0x4D,0x33,0x85,0x45,0xF9,0x02,0x7F,0x50,0x3C,0x9F,0xA8,
    0x51,0xA3,0x40,0x8F,0x92,0x9D,0x38,0xF5,0xBC,0xB6,0xDA,0x21,0x10,0xFF,0xF3,0xD2,
    0xCD,0x0C,0x13,0xEC,0x5F,0x97,0x44,0x17,0xC4,0xA7,0x7E,0x3D,0x64,0x5D,0x19,0x73,
    0x60,0x81,0x4F,0xDC,0x22,0x2A,0x90,0x88,0x46,0xEE,0xB8,0x14,0xDE,0x5E,0x0B,0xDB,
    0xE0,0x32,0x3A,0x0A,0x49,0x06,0x24,0x5C,0xC2,0xD3,0xAC,0x62,0x91,0x95,0xE4,0x79,
    0xE7,0xC8,0x37,0x6D,0x8D,0xD5,0x4E,0xA9,0x6C,0x56,0xF4,0xEA,0x65,0x7A,0xAE,0x08,
    0xBA,0x78,0x25,0x2E,0x1C,0xA6,0xB4,0xC6,0xE8,0xDD,0x74,0x1F,0x4B,0xBD,0x8B,0x8A,
    0x70,0x3E,0xB5,0x66,0x48,0x03,0xF6,0x0E,0x61,0x35,0x57,0xB9,0x86,0xC1,0x1D,0x9E,
    0xE1,0xF8,0x98,0x11,0x69,0xD9,0x8E,0x94,0x9B,0x1E,0x87,0xE9,0xCE,0x55,0x28,0xDF,
    0x8C,0xA1,0x89,0x0D,0xBF,0xE6,0x42,0x68,0x41,0x99,0x2D,0x0F,0xB0,0x54,0xBB,0x16,
]
_AES_RCON = [0x01,0x02,0x04,0x08,0x10,0x20,0x40,0x80,0x1B,0x36,0x6C,0xD8,0xAB,0x4D]


def _aes_xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _aes_key_expand(key: bytes):
    nk = len(key) // 4
    rounds = nk + 6
    w = [list(key[i * 4:i * 4 + 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [_AES_SBOX[b] for b in t]
            t[0] ^= _AES_RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [_AES_SBOX[b] for b in t]
        w.append([a ^ b for a, b in zip(w[i - nk], t)])
    return w, rounds


def _aes_encrypt_block(block: bytes, w, rounds) -> bytes:
    # state 按列表示: cols[c][r] = 第 c 列第 r 行(AES 输入按列填充)
    cols = [list(block[c * 4:c * 4 + 4]) for c in range(4)]

    def add_rk(r):
        for c in range(4):
            k = w[r * 4 + c]
            for i in range(4):
                cols[c][i] ^= k[i]

    add_rk(0)
    for rnd in range(1, rounds + 1):
        for col in cols:
            for i in range(4):
                col[i] = _AES_SBOX[col[i]]
        # ShiftRows: 第 r 行循环左移 r -> 新列 c 取旧列 (c+r)%4
        cols = [[cols[(c + r) % 4][r] for r in range(4)] for c in range(4)]
        if rnd != rounds:  # MixColumns(末轮跳过)
            for col in cols:
                a = col[:]
                t = a[0] ^ a[1] ^ a[2] ^ a[3]
                col[0] = a[0] ^ t ^ _aes_xtime(a[0] ^ a[1])
                col[1] = a[1] ^ t ^ _aes_xtime(a[1] ^ a[2])
                col[2] = a[2] ^ t ^ _aes_xtime(a[2] ^ a[3])
                col[3] = a[3] ^ t ^ _aes_xtime(a[3] ^ a[0])
        add_rk(rnd)
    return bytes(cols[c][r] for c in range(4) for r in range(4))


def _gf_mul(x: int, y: int) -> int:
    # GF(2^128) 乘法, GCM 位序
    r = 0
    for i in range(127, -1, -1):
        if (x >> i) & 1:
            r ^= y
        y = (y >> 1) ^ (0xE1 << 120) if y & 1 else y >> 1
    return r


def _ghash(h: int, aad: bytes, ct: bytes) -> int:
    def blocks(data: bytes):
        for i in range(0, len(data), 16):
            b = data[i:i + 16]
            if len(b) < 16:
                b += b"\x00" * (16 - len(b))
            yield int.from_bytes(b, "big")
    y = 0
    for b in blocks(aad):
        y = _gf_mul(y ^ b, h)
    for b in blocks(ct):
        y = _gf_mul(y ^ b, h)
    tail = struct.pack(">QQ", len(aad) * 8, len(ct) * 8)
    return _gf_mul(y ^ int.from_bytes(tail, "big"), h)


def _aes256gcm_decrypt(key: bytes, nonce: bytes, ct: bytes, aad: bytes, tag: bytes) -> bytes:
    """AES-256-GCM 解密(认证失败抛 ValueError)。"""
    w, rounds = _aes_key_expand(key)
    h = int.from_bytes(_aes_encrypt_block(b"\x00" * 16, w, rounds), "big")
    counter0 = (int.from_bytes(nonce, "big") << 32) | 1
    j0 = _aes_encrypt_block(counter0.to_bytes(16, "big"), w, rounds)
    s = _ghash(h, aad, ct)
    if bytes(a ^ b for a, b in zip(j0, s.to_bytes(16, "big"))) != tag:
        raise ValueError("GCM 认证失败(auth tag mismatch)")
    hi = counter0 & ~0xFFFFFFFF
    ctr = counter0 & 0xFFFFFFFF
    out = bytearray()
    for i in range(0, len(ct), 16):
        ctr = (ctr + 1) & 0xFFFFFFFF
        ks = _aes_encrypt_block((hi | ctr).to_bytes(16, "big"), w, rounds)
        out.extend(a ^ b for a, b in zip(ct[i:i + 16], ks))
    return bytes(out)


def _aes256gcm_encrypt(key: bytes, nonce: bytes, pt: bytes, aad: bytes) -> tuple[bytes, bytes]:
    """AES-256-GCM 加密,返回 (ciphertext, tag)。与 _aes256gcm_decrypt 互逆。"""
    w, rounds = _aes_key_expand(key)
    h = int.from_bytes(_aes_encrypt_block(b"\x00" * 16, w, rounds), "big")
    counter0 = (int.from_bytes(nonce, "big") << 32) | 1
    hi = counter0 & ~0xFFFFFFFF
    ctr = counter0 & 0xFFFFFFFF
    out = bytearray()
    for i in range(0, len(pt), 16):
        ctr = (ctr + 1) & 0xFFFFFFFF
        ks = _aes_encrypt_block((hi | ctr).to_bytes(16, "big"), w, rounds)
        out.extend(a ^ b for a, b in zip(pt[i:i + 16], ks))
    ct = bytes(out)
    j0 = _aes_encrypt_block(counter0.to_bytes(16, "big"), w, rounds)
    s = _ghash(h, aad, ct)
    return ct, bytes(a ^ b for a, b in zip(j0, s.to_bytes(16, "big")))


def _lp(x: bytes) -> bytes:
    return struct.pack(">I", len(x)) + x


def _wb_build_aad(key_id: str, framing: str, suite: int = 1) -> bytes:
    magic, no = (b"WBEF1", 1) if framing == "file" else (b"WBEV1", 2)
    return (b"WB-AAD\x00" + b"\x01" + _lp(magic) + _lp(b"sym-v1")
            + struct.pack(">I", suite) + _lp(key_id.encode("ascii"))
            + bytes([no]) + b"\x00" + b"\x00")


def wb_unseal_field(envelope_b64: str) -> str:
    """解开 WorkBuddy at-rest 字段封套,返回明文字符串。"""
    secret_b64 = _load_wb_at_rest_key()
    if not secret_b64:
        raise RuntimeError(
            "缺少 WorkBuddy at-rest 解密密钥 —— 请在装有 WorkBuddy 的机器上运行一次"
            " `node extract_wbkey.mjs`(会把密钥写入 config.json 的 wb_at_rest_key),"
            "或设置环境变量 WORKBUDDY_AT_REST_KEY")
    env = json.loads(base64.b64decode(envelope_b64))
    if env.get("suite") != 1:
        raise ValueError(f"不支持的加密套件 suite={env.get('suite')}")
    protector = hashlib.sha256(secret_b64.encode("utf-8")).digest()
    key_id = hashlib.sha256(protector).hexdigest()[:16]
    if env.get("keyId") != key_id:
        raise ValueError(
            f"envelope keyId {env.get('keyId')} 与本地推导的 {key_id} 不一致 —— "
            f"WorkBuddy 可能更换了内置密钥,请重新运行 extract_wbkey.mjs 提取,"
            f"并更新 config.json 里的 wb_at_rest_key")
    plain = _aes256gcm_decrypt(
        protector,
        base64.b64decode(env["nonce"]),
        base64.b64decode(env["ciphertext"]),
        _wb_build_aad(key_id, "field", env["suite"]),
        base64.b64decode(env["authTag"]),
    )
    return plain.decode("utf-8")


# --------------------------------------------------------------------------
# 本地账号库(多账号支持)
#
# 客户端切换账号时会直接覆盖本地登录态文件,旧账号的 token 随之丢失。
# 因此这里维护一个本地账号库 accounts.local.json(已加入 .gitignore):
# 每次运行先把客户端"当前"登录态截留入库(按账号 ID 去重、保留更新的
# token),签到时遍历库里全部账号,实现一台机器签多个账号。
#
# 注意: WorkBuddy 的非当前账号 token 无法自动续期(刷新凭证在客户端
# daemon 里),约 55 天过期 —— 届时在客户端把该账号切回去一次即可自动
# 续上并重新入库。Trae 的续期不受此限制,见下文 maintain_trae_accounts。
# --------------------------------------------------------------------------

STORE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "accounts.local.json")


def _load_store() -> dict:
    default = {"version": 2, "workbuddy": {}, "trae": {}, "qoder": {}}
    try:
        with open(STORE_PATH, "r", encoding="utf-8") as fh:
            store = json.load(fh)
        if isinstance(store, dict) and isinstance(store.get("workbuddy"), dict) \
                and isinstance(store.get("trae"), dict):
            store.setdefault("qoder", {})
            return store
    except Exception:
        pass
    return default


def _save_store(store: dict) -> None:
    store["version"] = 2
    tmp = STORE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, STORE_PATH)


def _store_upsert(store: dict, platform: str, key: str, entry: dict) -> bool:
    """按 key 入库;已有记录时只接受"更新签发"的登录态。

    两条保护: 过期 token 永不覆盖库里尚有价值的记录(例如用 refreshToken
    手工救回的会话);iat 更旧的 token 不覆盖更新的。
    """
    bucket = store.setdefault(platform, {})
    old = bucket.get(key)
    if old:
        keepsake = bool(old.get("access_token") or old.get("refresh_token"))
        new_info = token_expiry(entry.get("access_token") or "")
        if keepsake and new_info and new_info["days_left"] <= 0:
            return False
        if old.get("access_token") and new_iat_of(entry) <= int(old.get("iat") or 0):
            return False
    bucket[key] = entry
    return True


def new_iat_of(entry: dict) -> int:
    return int(entry.get("iat") or 0)


def load_config_token() -> tuple[str | None, str | None]:
    """从脚本同级的 config.json 读取手动配置的 WorkBuddy token(可选)。

    正常情况下本地模式自动从客户端登录态截留,无需手动配置;
    当客户端文件不可用(如云端/无客户端机器)时,把明文 access_token
    填进 config.json 即可继续签到。
    """
    cfg = _read_config()
    token = cfg.get("access_token")
    if token:
        log(f"  WorkBuddy token 来源: config.json(手动配置)")
    return token, cfg.get("uid")


def token_expiry(token: str | None) -> dict | None:
    """解析 JWT 的 exp/iat,返回 {exp, iat, days_left}。非 JWT 返回 None。"""
    if not token or token.count(".") != 2:
        return None
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        exp = payload.get("exp")
        iat = payload.get("iat")
        if not exp:
            return None
        return {
            "exp": exp,
            "iat": iat,
            "days_left": round((exp - time.time()) / 86400, 1),
        }
    except Exception:
        return None


def report_token_health(label: str, token: str | None) -> None:
    """打印 token 剩余有效期;不足 7 天时给出醒目告警。"""
    info = token_expiry(token)
    if not info:
        return
    days = info["days_left"]
    if days < 0:
        log(f"  [{label}] ⚠️ token 已过期 {abs(days)} 天,需要重新登录/导出")
    elif days < 7:
        log(f"  [{label}] ⚠️ token 仅剩 {days} 天,请尽快重新导出并更新 Secrets")
        log(f"  [{label}]    提示:打开一次 {label} 客户端会自动续期;"
            f"然后运行 `python checkin.py --export` 重新导出")
    else:
        log(f"  [{label}] token 剩余 {days} 天")


def export_tokens(with_trae: bool = False) -> int:
    """导出当前本地登录态中的 token,便于更新 GitHub Secrets。

    只打印到 stdout,不写任何文件。复制输出填入 Secrets 即可。
    """
    print("=" * 60)
    print("导出当前登录态 token(用于更新 GitHub Actions Secrets)")
    print("=" * 60)

    ok = True

    wb_token, wb_uid = read_workbuddy_token()
    print("\n[WorkBuddy]")
    print(f"  WORKBUDDY_ACCESS_TOKEN = {wb_token or '(未找到)'}")
    print(f"  WORKBUDDY_UID          = {wb_uid or '(未找到)'}")
    print("  WORKBUDDY_DOMAIN       = www.codebuddy.cn")
    if wb_token:
        info = token_expiry(wb_token)
        if info:
            print(f"  # 剩余 {info['days_left']} 天")
    else:
        ok = False

    if with_trae:
        tr_token, tr_region, _tr_uid = read_trae_token()
        print("\n[Trae CN]")
        print(f"  TRAE_TOKEN  = {tr_token or '(未找到)'}")
        print(f"  TRAE_REGION = {tr_region or 'CN'}")
        if tr_token:
            info = token_expiry(tr_token)
            if info:
                print(f"  # 剩余 {info['days_left']} 天")
        else:
            ok = False

    # 账号库里的其它账号(多账号场景;token 需要哪个就复制哪个)
    store = _load_store()
    for platform, keyname in (("workbuddy", "WORKBUDDY_ACCESS_TOKEN"),
                              ("trae", "TRAE_TOKEN"),
                              ("qoder", "QODER_TOKEN")):
        accounts = store.get(platform) or {}
        extra = {k: a for k, a in accounts.items()
                 if (a.get("access_token") or a.get("token"))}
        if len(extra) > 1 or (extra and not with_trae and platform == "trae"):
            print(f"\n[账号库·{platform}] 共 {len(extra)} 个账号:")
            for k, a in sorted(extra.items()):
                tok = a.get("access_token") or a.get("token") or ""
                info = token_expiry(tok)
                if info:
                    days = f"剩余 {info['days_left']} 天"
                else:
                    exp = _parse_iso_utc(a.get("expires_at"))
                    days = (f"至 {a.get('expires_at')}" if exp else "?")
                print(f"  {a.get('name') or k}  ({days})")
                print(f"    {keyname} = {tok}")

    print("\n" + "=" * 60)
    print("把上面的值填到仓库 Settings → Secrets and variables → Actions")
    print("=" * 60)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# AES 支持:优先 pycryptodome,否则回退纯 Python
# --------------------------------------------------------------------------

try:
    from Crypto.Cipher import AES as _PyCryptoAES  # type: ignore

    def aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
        return _PyCryptoAES.new(key, _PyCryptoAES.MODE_CBC, iv).decrypt(data)

    AES_BACKEND = "pycryptodome"
except Exception:
    try:
        import aes_fallback as _pure_aes  # type: ignore

        aes_cbc_decrypt = _pure_aes.aes_cbc_decrypt
        AES_BACKEND = "pure-python"
    except Exception:
        aes_cbc_decrypt = None  # type: ignore
        AES_BACKEND = "unavailable"


# --------------------------------------------------------------------------
# Trae CN 登录态解密
# --------------------------------------------------------------------------

HP = 16
Q8_AES128 = 16
WP = HP
RH = 64
RV = 32
VP = 64
EM = 6

_URE = bytes([
    82, 9, 106, 213, 48, 54, 165, 56, 191, 64, 163, 158, 129, 243, 215, 251,
    124, 227, 57, 130, 155, 47, 255, 135, 52, 142, 67, 68, 196, 222, 233, 203,
    84, 123, 148, 50, 166, 194, 35, 61, 238, 76, 149, 11, 66, 250, 195, 78,
    8, 46, 161, 102, 40, 217, 36, 178, 118, 91, 162, 73, 109, 139, 209, 37,
])
_DRE = bytes([
    31, 221, 168, 51, 136, 7, 199, 49, 177, 18, 16, 89, 39, 128, 236, 95,
    96, 81, 127, 169, 25, 181, 74, 13, 45, 229, 122, 159, 147, 201, 156, 239,
    160, 224, 59, 77, 174, 42, 245, 176, 200, 235, 187, 60, 131, 83, 153, 97,
    23, 43, 4, 126, 186, 119, 214, 38, 225, 105, 20, 99, 85, 33, 12, 125,
])


def decrypt_trae_blob(b64_value: str) -> dict:
    """解密 Trae CN storage.json 中的 iCubeAuthInfo://icube.cloudide。

    容器布局(解码后):
        [0:4]   magic b"tc"
        [4]     version
        [5]     flags
        [6:38]  "salt"/随机 key 材料 (RV=32)
        [38:]   AES-128-CBC 密文
    密钥派生:
        sha  = SHA512(key_material)
        xor  = URE ^ DRE
        hash = SHA512(sha || xor)
        aesKey = hash[0:16]
        iv     = hash[16:32]
    明文:
        前 RH(64) 字节为头部,其后为 UTF-8 JSON
    """
    if aes_cbc_decrypt is None:
        raise RuntimeError(
            "缺少 AES 实现。请安装 pycryptodome: pip install pycryptodome"
        )

    t = base64.b64decode(b64_value)
    key_material = t[EM:EM + RV]
    sha = hashlib.sha512(key_material).digest()
    xor = bytes(a ^ b for a, b in zip(_URE, _DRE))[:VP]
    digest = hashlib.sha512(sha + xor).digest()
    aes_key = digest[:Q8_AES128]
    iv = digest[Q8_AES128:Q8_AES128 + WP]
    ct = t[RV + EM:]
    ct = ct[: len(ct) // 16 * 16]
    plain = aes_cbc_decrypt(aes_key, iv, ct)
    body = plain[RH:]
    text = body.decode("utf-8", "ignore")
    text = text[: text.rfind("}") + 1]
    return json.loads(text)


def encrypt_trae_blob(json_text: str) -> str:
    """decrypt_trae_blob 的逆运算(自动续期后把新登录态写回 storage.json 用)。

    信封 = header(6) + 随机 key 材料(32) + AES-128-CBC(SHA512(payload) || payload + PKCS7)
    header 固定 b"tc\\x05\\x10\\x00\\x00";派生方式与解密一致。
    算法与 trae-mate 的 encrypt_trae_auth_info 一致(其多开实例即用此方式
    写 storage.json,客户端可正常读取),已在本地完成 encrypt->decrypt 往返验证。
    """
    rnd = os.urandom(RV)
    secret = bytes(a ^ b for a, b in zip(_URE, _DRE))[:VP]
    digest = hashlib.sha512(hashlib.sha512(rnd).digest() + secret).digest()
    key, iv = digest[:Q8_AES128], digest[Q8_AES128:Q8_AES128 + WP]
    payload = json_text.encode("utf-8")
    data = hashlib.sha512(payload).digest() + payload
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    w, rounds = _aes_key_expand(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        blk = bytes(a ^ b for a, b in zip(data[i:i + 16], prev))
        prev = _aes_encrypt_block(blk, w, rounds)
        out += prev
    return base64.b64encode(b"tc" + bytes([5, 16, 0, 0]) + rnd + bytes(out)).decode("ascii")


# --------------------------------------------------------------------------
# Trae 伪设备身份与自动续期(参考 trae-mate 项目,派生算法已通过其测试向量)
# --------------------------------------------------------------------------

def _seeded_stream(seed: str, salt: str, nbytes: int) -> bytes:
    """确定性字节流: SHA256(f"{salt}:{seed}" || counter_be32) 连续拼接。"""
    data = f"{salt}:{seed}".encode("utf-8")
    out = b""
    i = 0
    while len(out) < nbytes:
        out += hashlib.sha256(data + struct.pack(">I", i)).digest()
        i += 1
    return out[:nbytes]


def _trae_device_digits(n: int, seed: str) -> str:
    return "".join(str(b % 10) for b in _seeded_stream(seed, "devid", n + 1)[:n])


def _trae_device_hex(n: int, seed: str) -> str:
    return _seeded_stream(seed, "sess", (n + 1) // 2).hex()[:n]


def _trae_market_uuid(seed: str) -> str:
    bs = bytearray(_seeded_stream(seed, "market", 16))
    bs[6] = (bs[6] & 0x0F) | 0x40   # UUID v4 版本位
    bs[8] = (bs[8] & 0x3F) | 0x80   # RFC 4122 variant 位
    h = bs.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def trae_device_identity(user_id: str) -> dict:
    """按 user_id 确定性派生一套稳定的伪设备身份(trae-mate gen 2 算法)。

    同一 user_id 永远得到同一套标识,服务端按设备维度记账也保持稳定。
    测试向量(user_id="1234567890123456"): device_id=413174708280782,
    session_id=fbabf7aa1e173b90385c623e1ec49157860cc07a4b01f53f7ca141b17d876eae,
    market_user_id=746608f9-7f37-4960-b4c7-8553cec6d366。
    """
    return {
        "device_id": _trae_device_digits(15, user_id),
        "market_user_id": _trae_market_uuid(user_id),
        "session_id": _trae_device_hex(64, user_id),
    }


def build_trae_headers(token: str, user_id: str) -> dict:
    """Trae 签到接口的完整请求头(逆向自 trae-mate checkin.rs)。

    旧版只带 x-device-id 等零星头,领取接口常返回 9074 限流;
    完整的客户端伪装头 + 稳定伪设备身份是稳定领取的关键。
    x-request-id / x-tt-trace-id 每次请求刷新。
    """
    dev = trae_device_identity(user_id or "")
    return {
        "authorization": f"Cloud-IDE-JWT {token}",
        "accept": "*/*",
        "accept-encoding": "gzip, deflate",
        "accept-language": "zh-CN",
        "content-type": "application/json",
        "user-agent": "VSCode 1.107.1 (TRAE SOLO CN)",
        "x-market-client-id": "VSCode 1.107.1",
        "x-market-user-id": dev["market_user_id"],
        "x-user-region": "CN",
        "x-device-id": dev["device_id"],
        "x-lgw-req-sdk-type": "3",
        "package-type": "stable_cn",
        "x-lscbd-aid": "787976",
        "x-lscbd-platform": "windows",
        "app-version": "0.1.45",
        "x-tt-trace-id": "00-" + uuid.uuid4().hex[:16] + "-01",
        "vscode-sessionid": dev["session_id"],
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
        "x-request-id": str(uuid.uuid4()),
    }


def trae_exchange_refresh_token(refresh_token: str) -> tuple[str | None, str | None, str | None]:
    """用 refreshToken 换新 JWT。返回 (new_token, new_refresh_token, error)。

    参考 trae-mate jwt.rs 的发现:对 SOLO ClientID 用极简参数即可刷新,
    无需客户端那套 DeviceInfo/DeviceProof 设备签名(此前 README 记录的
    「设备绑定无法绕过」结论是端点/参数不对所致)。
    实测响应为腾讯网关信封 {"Result": {"Token", "RefreshToken", "RefreshExpireAt"}},
    同时兼容 {"code":0,"data":{"access_token"/"token","refresh_token"}} 形态。
    """
    body = json.dumps({
        "ClientID": TRAE_CLIENT_ID,
        "RefreshToken": refresh_token,
        "ClientSecret": "-",
        "UserID": "",
    }).encode("utf-8")
    req = urllib.request.Request(
        TRAE_EXCHANGE_URL, data=body,
        headers={"content-type": "application/json", "accept": "*/*"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8", "ignore"))
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "ignore")[:160]
        except Exception:
            pass
        return None, None, f"HTTP {exc.code}: {detail}"
    except Exception as exc:
        return None, None, str(exc)

    result = payload.get("Result") or {}
    data = payload.get("data") or {}
    new_token = result.get("Token") or data.get("access_token") or data.get("token")
    new_rt = result.get("RefreshToken") or data.get("refresh_token")
    if not new_token:
        err = (payload.get("ResponseMetadata") or {}).get("Error") or {}
        msg = (err.get("Message") or err.get("Code")
               or payload.get("message") or json.dumps(payload, ensure_ascii=False)[:160])
        return None, None, str(msg)
    return new_token, new_rt, None


def trae_write_back_auth(store_path: str, auth: dict) -> None:
    """把更新后的 auth 字典重新加密写回 profile 的 storage.json(原子替换)。"""
    with open(store_path, "r", encoding="utf-8") as fh:
        store = json.load(fh)
    store[TRAE_KEY] = encrypt_trae_blob(json.dumps(auth, ensure_ascii=False, separators=(",", ":")))
    tmp = store_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(store, fh, ensure_ascii=False)
    os.replace(tmp, store_path)


# --------------------------------------------------------------------------
# Windows 系统通知( toast;CHECKIN_NOTIFY=0 可关闭 )
#
# 用 PowerShell 调 Windows Runtime 的 ToastNotificationManager 发系统通知,
# 无需任何第三方模块。仅 Windows 弹出;其它平台/发送失败一律静默。
# --------------------------------------------------------------------------

_TOAST_PS = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastText02"><text id="1">{TITLE}</text><text id="2">{BODY}</text></binding></visual></toast>')
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
"""


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace("'", "&apos;"))


def notify(title: str, lines: list[str]) -> None:
    """发一条 Windows 系统通知(签到完成/失败提醒)。失败静默,不影响签到。"""
    if os.environ.get("CHECKIN_NOTIFY", "1").strip() in ("0", "false", "off"):
        return
    if sys.platform != "win32":
        return
    try:
        ps = (_QODER_TOAST_PS
              .replace("{TITLE}", _xml_escape(title))
              .replace("{BODY}", _xml_escape("\n".join(lines))))
        subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def _trae_client_running() -> bool:
    """TRAE 客户端是否在运行(运行中则跳过续期,避免与客户端互相覆盖登录态)。"""
    return _process_running("trae")


def harvest_trae_accounts(store: dict) -> None:
    """把各 profile 的 Trae 登录态截留入库(按 user_id 去重)。"""
    appdata = _appdata()
    if not appdata:
        return
    changed = False
    for rel in (TRAE_SOLO_STORAGE_REL, TRAE_STORAGE_REL):
        path = os.path.join(appdata, rel)
        if not os.path.isfile(path):
            continue
        name_dir = rel.split(os.sep)[0]
        try:
            with open(path, "r", encoding="utf-8") as fh:
                profile = json.load(fh)
            enc = profile.get(TRAE_KEY)
            if not enc:
                continue
            auth = decrypt_trae_blob(enc)
        except Exception as exc:
            log(f"  [账号库] {name_dir} 登录态解析失败: {exc}")
            continue
        token = auth.get("token") or ""
        uid = str(auth.get("userId") or "")
        if not token or not uid:
            continue
        tinfo = token_expiry(token)
        entry = {
            "name": (auth.get("account") or {}).get("username") or "",
            "access_token": token,
            "refresh_token": auth.get("refreshToken") or "",
            "region": (auth.get("userRegion") or {}).get("region") or "CN",
            "iat": int((tinfo or {}).get("iat") or 0),
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if _store_upsert(store, "trae", uid, entry):
            changed = True
            log(f"  [账号库] Trae 账号 {uid}({entry['name'] or '未命名'},来自 {name_dir}) 已入库/更新")
    if changed:
        _save_store(store)


def maintain_trae_accounts(store: dict) -> None:
    """按账号库做 Trae 自动续期(剩余不足 24h 的账号用 refreshToken 换新)。

    安全规则:
    * refreshToken 会轮换 —— 新值一律更新回账号库,绝不丢失;
    * 若该账号"当前"登录在某个 profile 里:客户端运行中则跳过(客户端自己
      会续,代刷会因轮换不同步导致客户端掉登录);客户端未运行则换新后
      同步写回该 profile 的 storage.json(仅当该 profile 里的会话就是
      我们刷的这个,以 refreshToken 一致为准);
    * 不在任何 profile 里的账号(纯账号库存档):直接换新,只更新库。
    """
    current: dict[str, dict] = {}  # uid -> {"path", "rt"}
    appdata = _appdata()
    if appdata:
        for rel in (TRAE_SOLO_STORAGE_REL, TRAE_STORAGE_REL):
            path = os.path.join(appdata, rel)
            if not os.path.isfile(path):
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    enc = json.load(fh).get(TRAE_KEY)
                if not enc:
                    continue
                auth = decrypt_trae_blob(enc)
                uid = str(auth.get("userId") or "")
                if uid:
                    current[uid] = {"path": path, "rt": auth.get("refreshToken") or ""}
            except Exception:
                pass

    running = _trae_client_running()
    changed = False
    for uid, acc in sorted((store.get("trae") or {}).items()):
        name = acc.get("name") or uid
        rt = acc.get("refresh_token") or ""
        token = acc.get("access_token") or ""
        info = token_expiry(token)
        days = info["days_left"] if info else None
        if not rt:
            log(f"  [{name}] 无 refreshToken,无法自动续期(token 剩余 {days if days is not None else '?'} 天)")
            continue
        if days is not None and days > 1.0:
            log(f"  [{name}] token 剩余 {days} 天,无需续期")
            continue
        if uid in current and running and current[uid].get("rt") == rt:
            log(f"  [{name}] TRAE 客户端运行中且持有该会话,跳过续期(由客户端负责)")
            continue
        new_token, new_rt, err = trae_exchange_refresh_token(rt)
        if err:
            log(f"  [{name}] ⚠️ 自动续期失败: {err}")
            log("           (若 refreshToken 已失效,需打开一次 TRAE 重新登录该账号)")
            continue
        new_info = token_expiry(new_token)
        # 校验新 JWT 属于同一用户,防止任何形式的串号
        try:
            pl_b64 = new_token.split(".")[1]
            pl = json.loads(base64.urlsafe_b64decode(pl_b64 + "=" * (-len(pl_b64) % 4)))
            new_uid = str((pl.get("data") or {}).get("id") or "")
            if new_uid and new_uid != str(uid):
                log(f"  [{name}] ⚠️ 刷新后 user_id 不匹配({new_uid} != {uid}),放弃")
                continue
        except Exception:
            pass
        acc["access_token"] = new_token
        if new_rt:
            acc["refresh_token"] = new_rt
        acc["iat"] = int((new_info or {}).get("iat") or 0)
        acc["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        changed = True
        log(f"  [{name}] ✅ 自动续期成功,新 token 剩余 {new_info['days_left'] if new_info else '?'} 天")

        cur = current.get(uid)
        if cur and not running and cur.get("rt") == rt:
            # 该 profile 里的就是这条会话 -> 同步写回,保持客户端与账号库一致
            try:
                with open(cur["path"], "r", encoding="utf-8") as fh:
                    profile = json.load(fh)
                auth = decrypt_trae_blob(profile[TRAE_KEY])
                auth["token"] = new_token
                auth["refreshToken"] = acc["refresh_token"]
                if new_info and new_info.get("exp"):
                    auth["expiredAt"] = time.strftime("%Y-%m-%dT%H:%M:%S",
                                                      time.gmtime(new_info["exp"])) + ".000Z"
                profile[TRAE_KEY] = encrypt_trae_blob(
                    json.dumps(auth, ensure_ascii=False, separators=(",", ":")))
                tmp = cur["path"] + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(profile, fh, ensure_ascii=False)
                os.replace(tmp, cur["path"])
                log(f"  [{name}] 已同步写回客户端登录态")
            except Exception as exc:
                log(f"  [{name}] ⚠️ 写回客户端登录态失败(账号库已是新值): {exc}")
    if changed:
        _save_store(store)


# --------------------------------------------------------------------------
# 读取登录态
# --------------------------------------------------------------------------

def _appdata() -> str | None:
    return os.environ.get("APPDATA")


def read_workbuddy_token() -> tuple[str | None, str | None]:
    """返回 (access_token, uid)。优先环境变量,其次本地登录态文件。

    本地文件中 auth.lastRefreshTime 是客户端最近一次自动刷新的时间戳,
    正常使用 WorkBuddy 时该值会被持续更新 —— 说明 token 一直保持新鲜。
    """
    env_token = os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    env_uid = os.environ.get("WORKBUDDY_UID")
    if env_token:
        log("  WorkBuddy token 来源: 环境变量(云端模式,不会自动续期)")
        return env_token, env_uid

    # 兜底 1:手动填的 config.json
    cfg_token, cfg_uid = load_config_token()
    if cfg_token:
        return cfg_token, cfg_uid or env_uid

    # 兜底 2:本地登录态文件
    # Windows 上该文件位于 LOCALAPPDATA;macOS 位于 Application Support
    roots = []
    for var in ("LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(var)
        if v:
            roots.append(v)
    home = os.path.expanduser("~")
    roots.append(os.path.join(home, "Library", "Application Support"))

    for root in roots:
        path = os.path.join(root, WORKBUDDY_AUTH_REL)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            auth = info.get("auth") or {}
            account = info.get("account") or {}
            last_refresh = auth.get("lastRefreshTime")
            hint = ""
            if isinstance(last_refresh, (int, float)) and last_refresh > 0:
                age_h = (time.time() * 1000 - last_refresh) / 1000 / 3600
                hint = f",客户端最近刷新: {age_h:.1f} 小时前"
            log(f"  WorkBuddy 登录态: {path}{hint}")

            token = auth.get("accessToken")
            if is_encrypted_field(token):
                # 5.6.2 起本地 token 是密文;密钥编译期内嵌在客户端里,
                # 我们直接离线解开,无需用户手动操作。
                try:
                    token = wb_unseal_field(token["envelope"])
                    log("  本地 token 为密文,已离线解密(keyId 校验通过)")
                except Exception as exc:
                    log(f"  ⚠️ 本地凭据解密失败: {exc}")
                    log("     兜底方案: 把明文 token 填进 config.json,"
                        "或设置环境变量 WORKBUDDY_ACCESS_TOKEN")
                    return None, account.get("uid")
            return token, account.get("uid")
        except Exception as exc:
            log(f"  WorkBuddy 登录态解析失败: {exc}")
    log(f"  未找到 WorkBuddy 登录态文件(已尝试 {len(roots)} 个位置)")
    return None, None


def _jwt_iat(token: str | None) -> int:
    """取 JWT 的 iat(签发时间戳),失败返回 0。用于挑选最新的 token。"""
    info = token_expiry(token)
    if not info:
        return 0
    return int(info.get("iat") or 0)


def read_trae_token() -> tuple[str | None, str | None, str | None]:
    """返回 (Cloud-IDE-JWT, region, user_id)。优先环境变量,其次本地 storage.json。

    注意:Trae 可能同时存在多份 profile 目录(`TRAE SOLO CN` / `Trae CN`),
    各自独立刷新 token。这里扫描所有候选文件,解密后**按签发时间(JWT.iat)
    挑最新的一枚**,确保拿到的是刚续期过的新 token。user_id 用于派生伪设备身份。
    """
    env_token = os.environ.get("TRAE_TOKEN")
    env_region = os.environ.get("TRAE_REGION")
    if env_token:
        log("  Trae token 来源: 环境变量(云端模式,不会自动续期)")
        return env_token, env_region, os.environ.get("TRAE_USER_ID", "")

    appdata = _appdata()
    if not appdata:
        return None, None, None

    candidates = [
        os.path.join(appdata, TRAE_SOLO_STORAGE_REL),
        os.path.join(appdata, TRAE_STORAGE_REL),
    ]

    best = None  # (iat, path, token, region, user_id)
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                store = json.load(fh)
            enc = store.get(TRAE_KEY)
            if not enc:
                continue
            auth = decrypt_trae_blob(enc)
            token = auth.get("token")
            if not token:
                continue
            region = (auth.get("userRegion") or {}).get("region")
            user_id = str(auth.get("userId") or "")
            iat = _jwt_iat(token)
            mtime = os.path.getmtime(path)
            log(f"  Trae 候选: {os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(path))))}"
                f"  签发={time.strftime('%Y-%m-%d %H:%M', time.localtime(iat)) if iat else '?'}"
                f"  文件改动={time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}")
            if best is None or iat > best[0]:
                best = (iat, path, token, region, user_id)
        except Exception as exc:
            log(f"  {path} 解析失败: {exc}")

    if best is None:
        return None, None, None

    iat, path, token, region, user_id = best
    log(f"  Trae 采用最新 token: {path}")
    return token, region, user_id


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

def post_json(url: str, headers: dict, payload: dict | None = None) -> tuple[int, dict | None, str]:
    """POST JSON,返回 (status, json_or_none, raw_text)。"""
    data = json.dumps(payload or {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            try:
                return resp.status, json.loads(raw), raw
            except json.JSONDecodeError:
                return resp.status, None, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(raw), raw
        except json.JSONDecodeError:
            return exc.code, None, raw
    except Exception as exc:
        return -1, None, str(exc)


# 需要重试的业务码:服务端限流(稍后再试即可成功),不属于"确定性结果"
RETRYABLE_CODES = {9074, 9004}
# 服务端限流的等待策略:前几轮快重试,之后逐渐拉长(实测约 3 分钟可恢复)
RATE_LIMIT_WAIT = (25, 45)
RATE_LIMIT_LONG_WAIT = (60, 90)
# 连续限流多少轮后改用长等待
RATE_LIMIT_LONG_AFTER = 4


def _is_retryable(body: dict | None) -> bool:
    """判断响应是否需要重试(限流等暂时性失败)。"""
    if not isinstance(body, dict):
        return False
    code = body.get("code")
    try:
        return int(code) in RETRYABLE_CODES
    except (TypeError, ValueError):
        return False


def with_retry(fn, label: str, retry_on_business_error: bool = False,
               max_attempts: int | None = None):
    """对网络波动/服务端限流做重试。

    默认策略:拿到了可解析的 JSON 且业务码不是"限流类",就视为服务端已应答,
    直接返回交由调用方判断 —— 避免对「今天已签到」这类确定性结果无谓重试。

    例外:业务码属于 RETRYABLE_CODES(如 9074 限流)说明只是暂时不可用,
    应当等待后重试,而不是直接判失败。
    max_attempts 可覆盖全局 MAX_RETRY(Trae 领取用更少的次数,避免占用太久)。
    """
    attempts = max_attempts or MAX_RETRY
    last = None
    for attempt in range(1, attempts + 1):
        result = fn()
        status, body, raw = result
        last = result

        # 服务端已给出结构化应答 -> 不再重试(限流除外)
        if body is not None and not _is_retryable(body):
            return result
        if status == 200 and not _is_retryable(body):
            return result

        if attempt < attempts:
            if _is_retryable(body):
                # 连续限流若干轮后拉长间隔,给服务端冷却时间
                # 实测:9074 通常在 2~4 分钟内恢复
                wait = random.randint(
                    *(RATE_LIMIT_LONG_WAIT if attempt > RATE_LIMIT_LONG_AFTER
                      else RATE_LIMIT_WAIT)
                )
                hint = "服务端限流"
            else:
                wait = random.randint(RETRY_WAIT_MIN, RETRY_WAIT_MAX)
                hint = f"status={status}"
            log(f"  {label}: {hint} raw={raw[:120]} -> 第 {attempt}/{attempts} 次重试,{wait}s 后...")
            time.sleep(wait)
    return last


# --------------------------------------------------------------------------
# WorkBuddy 签到
# --------------------------------------------------------------------------

def harvest_workbuddy_accounts(store: dict) -> None:
    """把客户端"当前"登录态里的 WorkBuddy 账号截留入库。

    客户端切换账号会覆盖 workbuddy-desktop.info,这里按 uid 留档,
    切换回来时 token 仍是库里最新的那份(客户端刷新时会一并更新)。
    """
    roots = []
    for var in ("LOCALAPPDATA", "APPDATA"):
        v = os.environ.get(var)
        if v:
            roots.append(v)
    home = os.path.expanduser("~")
    roots.append(os.path.join(home, "Library", "Application Support"))
    changed = False
    for root in roots:
        path = os.path.join(root, WORKBUDDY_AUTH_REL)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as fh:
                info = json.load(fh)
            account = info.get("account") or {}
            auth = info.get("auth") or {}
            uid = str(account.get("uid") or "")
            token = auth.get("accessToken")
            if is_encrypted_field(token):
                token = wb_unseal_field(token["envelope"])
            if not uid or not token:
                continue
            tinfo = token_expiry(token)
            nick = account.get("nickname")
            name = ""
            if is_encrypted_field(nick):
                try:
                    name = wb_unseal_field(nick["envelope"])
                except Exception:
                    name = ""
            entry = {
                "name": name,
                "access_token": token,
                "iat": int((tinfo or {}).get("iat") or 0),
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            if _store_upsert(store, "workbuddy", uid, entry):
                changed = True
                log(f"  [账号库] WorkBuddy 账号 {uid[:8]}({name or '未命名'}) 已入库/更新")
        except Exception as exc:
            log(f"  [账号库] WorkBuddy 登录态截留失败: {exc}")
    if changed:
        _save_store(store)


def _checkin_workbuddy_token(token: str, label: str) -> bool:
    """对单个 WorkBuddy 账号执行签到(状态预检 + 领取,幂等)。"""
    log(f"  --- 账号 {label} ---")
    log(f"  accessToken: {mask(token)}")
    report_token_health(f"WorkBuddy/{label}", token)

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    status, body, raw = with_retry(
        lambda: post_json(WORKBUDDY_STATUS_URL, headers), "查询签到状态"
    )
    if status != 200 or body is None:
        log(f"  查询状态失败: status={status} raw={raw[:200]}")
        return False

    data = body.get("data") or {}
    if data.get("today_checked_in"):
        log("  今日已签到(本次运行前已签过,未重复领取)")
        return True

    status, body, raw = with_retry(
        lambda: post_json(WORKBUDDY_CLAIM_URL, headers), "领取签到"
    )
    if body is not None:
        code = body.get("code")
        msg = body.get("msg", "")
        if code == 0:
            credit = (body.get("data") or {}).get("credit", "?")
            streak = (body.get("data") or {}).get("streak_days", "?")
            log(f"  签到成功!(本次运行完成了签到) +{credit} 积分,连续 {streak} 天")
            return True
        # 10001 / "已签到" 说明今天已经签过 —— 视为成功(幂等)
        if code == 10001 or "已签到" in msg:
            log(f"  今日已签到(本次运行前已签过,未重复领取): {msg or 'code=10001'}")
            return True
        log(f"  签到返回异常: code={code} msg={msg}")
        return False

    log(f"  领取失败: status={status} raw={raw[:200]}")
    return False


def checkin_workbuddy() -> bool:
    log("=== WorkBuddy 签到 ===")

    env_token = os.environ.get("WORKBUDDY_ACCESS_TOKEN")
    if env_token:
        # 云端模式:只用环境变量里的单账号,不碰本地账号库
        log("  WorkBuddy token 来源: 环境变量(云端模式,不会自动续期)")
        uid = os.environ.get("WORKBUDDY_UID", "")
        return _checkin_workbuddy_token(env_token, uid[:8] if uid else "云端账号")

    store = _load_store()
    harvest_workbuddy_accounts(store)

    pool: dict[str, dict] = {}
    cfg_token, cfg_uid = load_config_token()
    if cfg_token:
        pool[cfg_uid or cfg_token[:20]] = {"name": "config.json手动", "access_token": cfg_token}
    for uid, acc in (store.get("workbuddy") or {}).items():
        pool.setdefault(uid, {
            "name": acc.get("name") or uid[:8],
            "access_token": acc.get("access_token"),
        })

    if not pool:
        log("  跳过:未找到任何 WorkBuddy 账号(可设置 WORKBUDDY_ACCESS_TOKEN)")
        return False

    log(f"  共 {len(pool)} 个账号待签")
    results = []
    for key, acc in pool.items():
        token = acc.get("access_token")
        label = acc.get("name") or key[:8]
        if not token:
            log(f"  --- 账号 {label} --- 跳过:库中无 token")
            results.append(False)
            continue
        try:
            results.append(_checkin_workbuddy_token(token, label))
        except Exception as exc:
            log(f"  账号 {label} 执行异常: {exc}")
            results.append(False)
    return all(results) if results else False


# --------------------------------------------------------------------------
# Trae CN 签到(默认执行;--no-trae 可跳过)
#
# 2026-09-29 升级(参考 trae-mate 项目):
#   * 完整的客户端伪装请求头 + 按 user_id 派生的稳定伪设备身份,
#     替代旧版的零星设备头 —— 旧版常被 9074 限流,新版实测稳定;
#   * 签到前后自动续期(maintain_trae_tokens),token 快过期时用
#     refreshToken 换新并加密写回登录态,不再依赖打开客户端。
# --------------------------------------------------------------------------

def query_trae_credits(token: str) -> None:
    """查询剩余积分与最早过期时间(尽力而为,失败不影响签到结果)。"""
    headers = {
        "authorization": f"Cloud-IDE-JWT {token}",
        "content-type": "application/json",
        "accept": "*/*",
    }
    try:
        status, body, _raw = post_json(TRAE_CREDITS_URL, headers,
                                       {"require_usage": True, "req_source": 2})
        packs = (body or {}).get("user_entitlement_pack_list")
        if status != 200 or not isinstance(packs, list):
            return
        total, earliest = 0.0, None
        now = time.time()
        for pack in packs:
            limit = ((((pack or {}).get("entitlement_base_info") or {}).get("quota") or {})
                     .get("credits_limit"))
            if limit is None:
                continue
            used = (((pack or {}).get("usage") or {}).get("credits_amount")) or 0
            total += max(0.0, float(limit) - float(used))
            exp = (pack or {}).get("expire_time")
            if isinstance(exp, (int, float)) and exp > now:
                earliest = exp if earliest is None else min(earliest, exp)
        exp_txt = ""
        if earliest:
            exp_txt = f",最早一批 {time.strftime('%Y-%m-%d', time.localtime(earliest))} 过期"
        log(f"  当前剩余积分: {total:g}{exp_txt}")
    except Exception:
        pass


def _trae_api_ok(body: dict | None) -> bool:
    """trae-mate 同款成功判定:兼容 code 0/200(数字或字符串)、success、status。"""
    if not isinstance(body, dict):
        return False
    code = body.get("code")
    if code in (0, 200, "0", "200"):
        return True
    if body.get("success") is True or body.get("status") == "success":
        return True
    return False


def _checkin_trae_token(token: str, region: str, user_id: str, name: str) -> bool:
    """对单个 Trae 账号执行签到(状态预检 + 领取,幂等)。"""
    log(f"  --- 账号 {name} ---")
    log(f"  token: {mask(token)} region={region}")
    report_token_health(f"Trae/{name}", token)

    headers = build_trae_headers(token, user_id)
    if region and region != "CN":
        headers["x-user-region"] = region
    dev = trae_device_identity(user_id or "")
    log(f"  伪设备身份: x-device-id={dev['device_id']}")
    payload = {}  # 客户端实测 body 为空对象

    # 1) status 预检:已签到则跳过领取
    status, body, raw = with_retry(
        lambda: post_json(TRAE_STATUS_URL, headers, payload), "查询签到状态",
        max_attempts=3,
    )
    if status == 200 and isinstance(body, dict):
        if body.get("checked_in"):
            log(f"  今日已签到(本次运行前已签过,未重复领取),"
                f"积分 {body.get('credits')}(+{body.get('extra_credits')})")
            query_trae_credits(token)
            return True
        if body.get("enable") is False:
            log("  签到活动未开启")
            return True
    else:
        log(f"  查询状态失败: status={status} raw={raw[:160]}(仍尝试领取)")

    # 2) 领取(9074 限流时短暂重试;业务失败不重试)
    status, body, raw = with_retry(
        lambda: post_json(TRAE_CLAIM_URL, headers, payload), "领取签到",
        max_attempts=max(1, int(os.environ.get("CHECKIN_TRAE_MAX_RETRY", "4"))),
    )
    if _trae_api_ok(body):
        log("  签到成功!(本次运行完成了签到) 响应: "
            + json.dumps(body, ensure_ascii=False)[:150])
        query_trae_credits(token)
        return True
    if isinstance(body, dict):
        msg = body.get("message") or body.get("msg") or ""
        code = body.get("code")
        if "已签到" in str(msg):
            log(f"  今日已签到(本次运行前已签过,未重复领取): {msg}")
            query_trae_credits(token)
            return True
        log(f"  签到返回异常: code={code} msg={msg}")
        return False

    log(f"  领取失败: status={status} raw={raw[:200]}")
    return False


def checkin_trae() -> bool:
    log("=== Trae CN 签到 ===")
    store = _load_store()
    harvest_trae_accounts(store)
    maintain_trae_accounts(store)

    accounts: dict[str, dict] = dict(store.get("trae") or {})
    env_token = os.environ.get("TRAE_TOKEN")
    if env_token:
        # 云端模式:环境变量单账号,不落盘、不续期
        uid = ""
        try:
            pl_b64 = env_token.split(".")[1]
            pl = json.loads(base64.urlsafe_b64decode(pl_b64 + "=" * (-len(pl_b64) % 4)))
            uid = str((pl.get("data") or {}).get("id") or "")
        except Exception:
            pass
        accounts = {uid or "cloud": {
            "name": "云端账号", "access_token": env_token,
            "region": os.environ.get("TRAE_REGION", "CN"),
        }}

    if not accounts:
        # 本机没有 Trae 登录态(如云端模式)不算失败,中性跳过
        log("  跳过:未找到 Trae 登录态/token(可设置 TRAE_TOKEN)")
        return True

    log(f"  共 {len(accounts)} 个账号待签")
    results = []
    for uid, acc in sorted(accounts.items()):
        token = acc.get("access_token") or ""
        name = acc.get("name") or uid
        info = token_expiry(token)
        if not token or (info and info["days_left"] <= 0):
            log(f"  --- 账号 {name} --- 跳过:token 已过期且无法续期")
            results.append(False)
            continue
        try:
            results.append(_checkin_trae_token(token, acc.get("region") or "CN",
                                               uid, name))
        except Exception as exc:
            log(f"  账号 {name} 执行异常: {exc}")
            results.append(False)
    return all(results) if results else True


# --------------------------------------------------------------------------
# Qoder 签到(2026-10 接入,接口逆向自客户端;参考社区 qoder-checkin 项目)
#
# 凭据存放(与 TRAE 完全不同): 客户端数据目录 %APPDATA%\com.qoder*.app.*\
#   * auth.v1.dat     = Chromium os_crypt 格式: b"v10" + nonce(12) + AES-256-GCM 密文,
#                       明文为 JSON {token:"dt-..", refreshToken:"drt-..", expiresAt, user{...}}
#                       密钥在 Local State 的 os_crypt.encrypted_key(DPAPI 保护,剥 5 字节前缀)
#   * auth.machine-id = Cosy-MachineId
# ⚠️ DPAPI 解 key 必须走 PowerShell 子进程(.NET ProtectedData) —— 本机实测
#    python 直接调 Crypt*Data 会被 WorkBuddy 行为防护终止进程(见 README)。
# 接口(CN 域 openapi.qoder.com.cn / 国际域 openapi.qoder.sh):
#   GET  /sash/api/v1/me/campaigns              查活动(Authorization: Bearer dt-..)
#   POST /sash/api/v1/me/campaigns/{cid}/claim  领取(data.status == "CLAIMED")
#   POST /api/v1/deviceToken/refresh            续期 {"refresh_token": drt-..}
#   请求头需带 Cosy-* 设备标识(runtime-info.exe 生成 machineToken/Code/Type)
# 活动每日 10:00 (UTC+8) 刷新,领取后 30 天有效,claim 幂等。
# --------------------------------------------------------------------------

QODER_UMID_REL = os.path.join("resources", "umid", "runtime-info.exe")


def _qoder_datadirs() -> list[str]:
    """客户端数据目录:国际版 com.qoder.app.* / 国内版 com.qodercn.app.*。"""
    appdata = os.environ.get("APPDATA", "")
    out = []
    for pat in ("com.qoder.app.*", "com.qodercn.app.*"):
        out += sorted(glob.glob(os.path.join(appdata, pat)),
                      key=os.path.getmtime, reverse=True)
    return out


def _qoder_variant(datadir: str) -> str:
    return "cn" if ".qodercn." in os.path.basename(datadir).lower() else "intl"


def _qoder_base(variant: str) -> str:
    return "https://openapi.qoder.com.cn" if variant == "cn" else "https://openapi.qoder.sh"


_QODER_PS_UNPROTECT = r"""
$ErrorActionPreference = 'Stop'
$datadir = '{DATA}'
$s = Get-Content (Join-Path $datadir 'Local State') -Raw | ConvertFrom-Json
$ek = [Convert]::FromBase64String($s.os_crypt.encrypted_key)
Add-Type -AssemblyName System.Security
$keyBytes = [byte[]]$ek[5..($ek.Length - 1)]
$key = [System.Security.Cryptography.ProtectedData]::Unprotect($keyBytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
[Convert]::ToBase64String($key)
"""


def _qoder_oscrypt_key(datadir: str) -> bytes:
    """经 PowerShell(.NET DPAPI) 解出 auth.v1.dat 的 AES-256 密钥。"""
    ps = _QODER_PS_UNPROTECT.replace("{DATA}", datadir.replace("'", "''"))
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-Command", ps], capture_output=True, text=True, timeout=60)
    out = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not out:
        raise OSError(f"PowerShell DPAPI 解 key 失败: {(r.stderr or r.stdout or '')[:160]}")
    return base64.b64decode(out[-1].strip())


def _qoder_decrypt_auth(datadir: str) -> dict:
    """解 auth.v1.dat,返回会话 JSON(token/refreshToken/expiresAt/user/...)。"""
    key = _qoder_oscrypt_key(datadir)
    raw = open(os.path.join(datadir, "auth.v1.dat"), "rb").read()
    if raw[:3] != b"v10":
        raise ValueError(f"auth.v1.dat 格式异常: {raw[:6]!r}")
    nonce, ct = raw[3:15], raw[15:]
    plain = _aes256gcm_decrypt(key, nonce, ct[:-16], b"", ct[-16:])
    return json.loads(plain.decode("utf-8"))


def _qoder_write_back_auth(datadir: str, session: dict) -> None:
    """把更新后的会话重新加密写回 auth.v1.dat(原子替换,保持客户端可读)。"""
    key = _qoder_oscrypt_key(datadir)
    payload = json.dumps(session, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(12)
    ct, tag = _aes256gcm_encrypt(key, nonce, payload, b"")
    tmp = os.path.join(datadir, "auth.v1.dat.tmp")
    with open(tmp, "wb") as fh:
        fh.write(b"v10" + nonce + ct + tag)
    os.replace(tmp, os.path.join(datadir, "auth.v1.dat"))


def _parse_iso_utc(s: str | None) -> float:
    """解析 ISO8601(含 Z/毫秒)为 Unix 时间戳,失败返回 0。"""
    if not s:
        return 0.0
    t = str(s).strip().replace("Z", "")
    try:
        if "." in t:
            t = t.split(".")[0]
        return float(calendar.timegm(time.strptime(t, "%Y-%m-%dT%H:%M:%S")))
    except Exception:
        return 0.0


def _process_running(keyword: str) -> bool:
    """按进程名关键字检查是否有进程在运行(Windows tasklist;其它平台返回 False)。

    tasklist 输出可能是 GBK 编码(含中文进程名),必须按字节捕获后容错解码,
    否则 text=True 的 UTF-8 解码会随机崩溃。
    """
    try:
        r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                           capture_output=True, timeout=15)
        out = (r.stdout or b"").decode("utf-8", "ignore").lower()
    except Exception:
        return False
    return keyword.lower() in out


def _qoder_client_running() -> bool:
    return _process_running("qoder")


def _qoder_umid_exe() -> str | None:
    """找 runtime-info.exe:注册表卸载项 InstallLocation 优先,环境变量可覆盖。"""
    import winreg
    cands = []
    env = os.environ.get("QODER_UMID_EXE", "").strip()
    if env:
        cands.append(env)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")
            n = winreg.QueryInfoKey(key)[0]
            for i in range(n):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        name = str(winreg.QueryValueEx(sub, "DisplayName")[0]).lower()
                        if "qoder" not in name:
                            continue
                        loc = str(winreg.QueryValueEx(sub, "InstallLocation")[0]).strip()
                        if loc:
                            cands.append(os.path.join(loc, QODER_UMID_REL))
                except OSError:
                    continue
            winreg.CloseKey(key)
        except OSError:
            pass
    for p in cands:
        if os.path.isfile(p):
            return p
    return None


def _qoder_device_info(exe: str | None) -> dict:
    """runtime-info.exe 生成 machineToken/machineCode/machineType(机器级,可缓存)。"""
    if not exe:
        return {}
    try:
        r = subprocess.run([exe, "--account-stdin"], input=b"",
                           capture_output=True, timeout=40, cwd=os.path.dirname(exe))
        out = r.stdout.decode("utf-8", "replace").strip()
        lines = [l for l in out.splitlines() if l.strip()]
        data = json.loads(lines[-1]) if lines else {}
        return {k: data.get(k) or "" for k in ("machineToken", "machineCode", "machineType")}
    except Exception:
        return {}


def _qoder_headers(acc: dict) -> dict:
    dev = acc.get("device") or {}
    arch = {"amd64": "x86_64", "x86_64": "x86_64"}.get(platform.machine().lower(), "x86_64")
    h = {
        "Authorization": f"Bearer {acc.get('token') or ''}",
        "Accept": "application/json",
        "User-Agent": "Qoder/claim",
        "Cosy-ClientType": "10",
        "Cosy-MachineOS": f"{arch}_windows",
        "Cosy-MachineHostname": os.environ.get("COMPUTERNAME", "pc"),
    }
    if acc.get("machine_id"):
        h["Cosy-MachineId"] = acc["machine_id"]
    for k in ("machineToken", "machineCode", "machineType"):
        if dev.get(k):
            h["Cosy-" + k[0].upper() + k[1:]] = dev[k]
    return h


def _qoder_req(base: str, path: str, headers: dict, body: dict | None = None) -> tuple[int, dict | None, str]:
    url = base.rstrip("/") + path
    data = None
    h = dict(headers)
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=h, method="POST" if body is not None else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", "ignore")
            return resp.status, (json.loads(raw) if raw.strip() else None), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "ignore")
        try:
            return exc.code, json.loads(raw), raw
        except Exception:
            return exc.code, None, raw
    except Exception as exc:
        return -1, None, str(exc)


def _qoder_pick_token(payload: dict) -> tuple[str, str]:
    """从续期响应里挖新 token 对,兼容多种信封。

    实测响应(2026-10-01,全 snake_case):
    {"device_token": "dt-..", "refresh_token": "drt-..", "token_type": "Bearer",
     "expires_at": "...", "refresh_token_expires_at": "...", "created_at": "..."}
    同时兼容 camelCase 及 {data:{...}} 信封。
    """
    stack = [payload]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        tok = (node.get("device_token") or node.get("token")
               or node.get("accessToken") or node.get("access_token"))
        rt = node.get("refresh_token") or node.get("refreshToken")
        if tok and isinstance(tok, str):
            return tok, (rt or "")
        stack.extend(v for v in node.values() if isinstance(v, dict))
    return "", ""


def harvest_qoder_accounts(store: dict) -> None:
    """把各客户端数据目录的 Qoder 会话截留入库(按 user_id 去重,expiresAt 新者胜)。"""
    if not _qoder_datadirs():
        return
    changed = False
    exe = None  # 惰性:只有需要补 device 时才找 runtime-info.exe
    for datadir in _qoder_datadirs():
        try:
            sess = _qoder_decrypt_auth(datadir)
        except Exception as exc:
            log(f"  [账号库] {os.path.basename(datadir)} Qoder 会话解密失败: {exc}")
            continue
        token = str(sess.get("token") or "")
        rt = str(sess.get("refreshToken") or "")
        if not token:
            continue
        user = sess.get("user") or {}
        uid = str(user.get("id") or "")
        if not uid:
            continue
        variant = _qoder_variant(datadir)
        exp = _parse_iso_utc(sess.get("expiresAt"))
        old = (store.setdefault("qoder", {}).get(uid) or {})
        if old.get("token") and _parse_iso_utc(old.get("expires_at")) >= exp:
            continue  # 库里的 token 更新(可能刚续期过),不动
        machine_id = ""
        try:
            machine_id = open(os.path.join(datadir, "auth.machine-id"),
                              encoding="utf-8").read().strip()
        except Exception:
            pass
        device = old.get("device") or {}
        if not device.get("machineToken"):
            if exe is None:
                exe = _qoder_umid_exe()
            device = _qoder_device_info(exe)
        entry = {
            "name": user.get("name") or user.get("phone") or uid[:8],
            "token": token,
            "refresh_token": rt,
            "expires_at": sess.get("expiresAt") or "",
            "refresh_expires_at": sess.get("refreshTokenExpiresAt") or "",
            "base": _qoder_base(variant),
            "variant": variant,
            "datadir": datadir,
            "machine_id": machine_id,
            "device": device,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store["qoder"][uid] = entry
        changed = True
        log(f"  [账号库] Qoder 账号 {entry['name']}({variant}) 已入库/更新,token 至 {entry['expires_at']}")
    if changed:
        _save_store(store)


def _qoder_force_refresh(acc: dict) -> bool:
    """对单个 Qoder 账号强制续期;成功则同步账号库并(条件满足时)写回客户端。"""
    name = acc.get("name") or (acc.get("_uid") or "")[:8]
    rt = acc.get("refresh_token") or ""
    if not rt:
        log(f"  [{name}] 无 refreshToken,无法续期")
        return False
    base = acc.get("base") or _qoder_base(acc.get("variant") or "cn")
    headers = _qoder_headers(acc)
    headers.pop("Authorization", None)
    status, body, raw = _qoder_req(base, "/api/v1/deviceToken/refresh", headers,
                                   {"refresh_token": rt})
    if status != 200 or not isinstance(body, dict):
        log(f"  [{name}] ⚠️ 自动续期失败: HTTP {status} {raw[:140]}")
        return False
    new_tok, new_rt = _qoder_pick_token(body)
    if not new_tok:
        log(f"  [{name}] ⚠️ 续期响应里没有 token: {json.dumps(body, ensure_ascii=False)[:140]}")
        return False
    acc["token"] = new_tok
    acc["refresh_token"] = new_rt or rt
    for key in ("expiresAt", "expires_at"):
        if isinstance(body.get(key), str):
            acc["expires_at"] = body[key]
            break
    if isinstance(body.get("expiresIn"), (int, float)):
        acc["expires_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(time.time() + body["expiresIn"]))
    for key in ("refreshTokenExpiresAt", "refresh_token_expires_at"):
        if isinstance(body.get(key), str):
            acc["refresh_expires_at"] = body[key]
            break
    acc["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")

    uid = acc.get("_uid")
    if uid:
        store = _load_store()
        if uid in (store.get("qoder") or {}):
            for k in ("token", "refresh_token", "expires_at", "refresh_expires_at", "updated_at"):
                if acc.get(k):
                    store["qoder"][uid][k] = acc[k]
            _save_store(store)

    datadir = acc.get("datadir") or ""
    if datadir and os.path.isfile(os.path.join(datadir, "auth.v1.dat")) \
            and not _qoder_client_running():
        try:
            sess = _qoder_decrypt_auth(datadir)
            if str(sess.get("refreshToken") or "") == rt:
                sess["token"] = acc["token"]
                sess["refreshToken"] = acc["refresh_token"]
                if acc.get("expires_at"):
                    sess["expiresAt"] = acc["expires_at"]
                if acc.get("refresh_expires_at"):
                    sess["refreshTokenExpiresAt"] = acc["refresh_expires_at"]
                _qoder_write_back_auth(datadir, sess)
                log(f"  [{name}] 已同步写回客户端登录态")
        except Exception as exc:
            log(f"  [{name}] ⚠️ 写回客户端登录态失败(账号库已是新值): {exc}")
    log(f"  [{name}] ✅ 自动续期成功,新 token 至 {acc.get('expires_at') or '(未知)'}")
    return True


def maintain_qoder_accounts(store: dict) -> None:
    """Qoder 自动续期:token 剩余不足 72h 时用 refreshToken 换新并写回 auth.v1.dat。"""
    running = _qoder_client_running()
    for uid, acc in sorted((store.get("qoder") or {}).items()):
        acc["_uid"] = uid
        name = acc.get("name") or uid[:8]
        rt = acc.get("refresh_token") or ""
        exp = _parse_iso_utc(acc.get("expires_at"))
        days = (exp - time.time()) / 86400 if exp else None
        if not rt:
            log(f"  [{name}] 无 refreshToken,无法自动续期(剩余 {days if days is not None else '?'} 天)")
            continue
        if days is not None and days > 3.0:
            log(f"  [{name}] token 剩余 {days:.1f} 天,无需续期")
            continue
        if running:
            log(f"  [{name}] Qoder 客户端运行中,跳过续期(由客户端负责)")
            continue
        _qoder_force_refresh(acc)


def _checkin_qoder_token(acc: dict, name: str) -> bool:
    log(f"  --- 账号 {name} ---")
    exp = _parse_iso_utc(acc.get("expires_at"))
    if exp:
        days = (exp - time.time()) / 86400
        log(f"  token 剩余 {days:.1f} 天")
        if days <= 0:
            log("  跳过:token 已过期且续期未成功")
            return False
    base = acc.get("base") or _qoder_base(acc.get("variant") or "cn")
    headers = _qoder_headers(acc)

    status, body, raw = with_retry(
        lambda: _qoder_req(base, "/sash/api/v1/me/campaigns", headers),
        "查询活动", max_attempts=3)
    if status == 401:
        log("  token 失效,尝试强制续期...")
        if _qoder_force_refresh(acc):
            headers = _qoder_headers(acc)
            status, body, raw = _qoder_req(base, "/sash/api/v1/me/campaigns", headers)
    if status != 200 or not isinstance(body, dict):
        log(f"  查询活动失败: HTTP {status} {raw[:160]}")
        return False

    campaigns = body.get("campaigns") or []
    # 每日签到识别: 领取类活动 + CREDITS 权益(参考 sun-olympic/qoder-checkin,
    # 避免误领订阅优惠等其它 CLAIM_BENEFIT 活动)
    benefits = [c for c in campaigns
                if isinstance(c, dict) and c.get("actionType") == "CLAIM_BENEFIT"
                and isinstance(c.get("benefit"), dict) and c["benefit"].get("kind") == "CREDITS"]
    claimable = [c for c in benefits if c.get("claimStatus") == "CLAIMABLE"]
    if not claimable:
        claimed_now = [c for c in benefits if c.get("claimStatus") == "CLAIMED"]
        if claimed_now:
            log(f"  今日已签到(本次运行前已领过,未重复领取),活动 {len(claimed_now)} 个")
            return True
        log(f"  ❌ 签到失败:服务端今日未下发可领取的签到活动(列表 {len(campaigns)} 项)——"
            f"可稍后在客户端活动页手动领取,或手动重跑本脚本再试")
        return False

    got, fail = 0.0, 0
    for c in claimable:
        cid = str(c.get("campaignId") or "")
        s2, d2, r2 = _qoder_req(base, f"/sash/api/v1/me/campaigns/{cid}/claim", headers, {})
        dd = d2.get("data") if isinstance(d2, dict) and isinstance(d2.get("data"), dict) else d2
        if s2 == 200 and isinstance(dd, dict) and dd.get("status") == "CLAIMED":
            amt = (dd.get("benefit") or {}).get("amount") or (c.get("benefit") or {}).get("amount") or 0
            got += float(amt) if isinstance(amt, (int, float)) else 0
        else:
            fail += 1
            log(f"  领取失败: {cid} HTTP {s2} {r2[:120]}")
    if fail:
        log(f"  签到部分失败: {len(claimable) - fail}/{len(claimable)} 个活动领取成功")
        return False
    log(f"  签到成功!(本次运行完成了签到) +{got:g} Credits,共 {len(claimable)} 个活动")
    return True


def checkin_qoder() -> bool:
    log("=== Qoder 签到 ===")
    if not _qoder_datadirs():
        log("  跳过:本机未安装 Qoder 客户端数据")
        return True
    store = _load_store()
    store.setdefault("qoder", {})
    harvest_qoder_accounts(store)
    maintain_qoder_accounts(store)
    accounts = store.get("qoder") or {}
    if not accounts:
        log("  跳过:未找到 Qoder 登录态(客户端登录一次后自动入库)")
        return True
    log(f"  共 {len(accounts)} 个账号待签")
    results = []
    for uid, acc in sorted(accounts.items()):
        acc["_uid"] = uid
        name = acc.get("name") or uid[:8]
        try:
            results.append(_checkin_qoder_token(acc, name))
        except Exception as exc:
            log(f"  账号 {name} 执行异常: {exc}")
            results.append(False)
    return all(results) if results else True


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(with_trae: bool = True, with_qoder: bool = True) -> int:
    _trim_log()
    log("=" * 50)
    log("每日自动签到开始 (WorkBuddy + Trae + Qoder)")
    log("=" * 50)

    targets: list[tuple[str, object]] = [("WorkBuddy", checkin_workbuddy)]
    if with_trae:
        log(f"[Trae] 签到启动(入库/续期在签到流程内进行),AES 后端: {AES_BACKEND}")
        targets.append(("Trae CN", checkin_trae))
    if with_qoder:
        log("[Qoder] 签到启动(入库/续期在签到流程内进行)")
        targets.append(("Qoder", checkin_qoder))

    results = []
    for name, fn in targets:
        try:
            results.append((name, fn()))
        except Exception as exc:
            log(f"  {name} 执行异常: {exc}")
            results.append((name, False))

    log("-" * 50)
    for name, ok in results:
        log(f"  {name}: {'成功' if ok else '未完成'}")
    log("=" * 50)

    # Windows 系统通知:签到完成/失败提醒
    ok_n = sum(1 for _, ok in results if ok)
    title = f"AiCheckin 签到{'完成' if ok_n == len(results) else '有失败'}"
    notify(title, [f"{name} {'✅' if ok else '❌ 请查看日志'}" for name, ok in results])

    # 全部成功 -> 0;有失败 -> 1,方便 CI 中观察
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    args = set(sys.argv[1:])
    with_trae = "--no-trae" not in args
    with_qoder = "--no-qoder" not in args

    if "--export" in args:
        sys.exit(export_tokens(with_trae))
    if "--help" in args or "-h" in args:
        print(__doc__)
        print(
            "选项:\n"
            "  --no-trae   跳过 Trae CN\n"
            "  --no-qoder  跳过 Qoder\n"
            "  --export    导出当前本地 token(用于更新 GitHub Secrets)"
        )
        sys.exit(0)
    sys.exit(main(with_trae, with_qoder))
