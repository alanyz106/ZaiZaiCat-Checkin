#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Trae CN relay 的 JWT 供给脚本（零第三方依赖，仅标准库）

为什么需要它（实测结论，2026-10-25）：
- trae2api-cn 镜像只认 `refreshToken`（走 ExchangeToken 自动续期）或手工粘贴的
  Cloud-IDE-JWT；它**没有** sessionid 入口。
- 而本仓库签到脚本手里只有 `sessionid`（长效 ~60 天 Cookie）。
- `POST /cloudide/api/v3/trae/Login` 只回吐新的 `X-Cloudide-Session` 与用户昵称，
  **不下发 refreshToken**，故无法把 relay 接到它自带的自动续期链路上。

因此本脚本承担「续期」职责：
  sessionid -> Login -> X-Cloudide-Session -> GetUserToken -> JWT（约 8 小时）
  -> POST {relay}/api/web-auth 热写入账号池（带 expiredAt，relay 据此判断有效期）

JWT 仅约 8 小时有效，故由 sidecar 容器每小时调用一次（见 README.md）。

只用标准库：这样同一份脚本既能跑在宿主机（/usr/bin/python3），又能跑在 relay
镜像内（该镜像只有 httpx，没有 requests）。

用法：
    python3 mint_jwt.py                       # 刷新并写入 relay
    python3 mint_jwt.py --dry-run             # 只换 JWT 打印信息，不写 relay
    python3 mint_jwt.py --relay-url URL ...   # 覆盖 relay 地址（容器内用服务名）
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT_URL = 'https://api.trae.cn'
LOGIN_PATH = '/cloudide/api/v3/trae/Login'
GET_TOKEN_PATH = '/cloudide/api/v3/common/GetUserToken'

# 官方桌面客户端（TRAE SOLO CN）真实 UA，与签到脚本保持一致
USER_AGENT = 'VSCode 1.107.1 (TRAE SOLO CN)'

TOKEN_JSON = HERE.parent / 'config' / 'token.json'
RELAY_ENV = HERE / '.env'
DEFAULT_RELAY_URL = 'http://127.0.0.1:18080'


def log(msg: str, quiet: bool = False) -> None:
    if not quiet:
        print(msg, flush=True)


def fail(msg: str) -> int:
    print(f'[ERROR] {msg}', file=sys.stderr, flush=True)
    return 1


def _post(url: str, headers: dict, body: bytes = b'{}', timeout: int = 30):
    """发 POST，返回 (status, headers, text)；不抛 HTTPError，便于读错误体"""
    request = urllib.request.Request(url, data=body, headers=headers, method='POST')
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode('utf-8', 'replace')


def read_sessionid(token_json: Path) -> str:
    """读取配置里的 trae sessionid（首个非空账号）"""
    if not token_json.is_file():
        raise RuntimeError(f'缺少配置文件: {token_json}')
    data = json.loads(token_json.read_text('utf-8'))
    accounts = (data.get('trae') or {}).get('accounts') or []
    for account in accounts:
        sessionid = str(account.get('sessionid') or '').strip()
        if sessionid:
            return sessionid
    raise RuntimeError('config/token.json 里没有 trae.sessionid')


def refresh_cloudide_session(sessionid: str) -> str:
    """用长效 sessionid 换取新的 X-Cloudide-Session"""
    status, headers, _ = _post(
        ROOT_URL + LOGIN_PATH,
        {
            'Cookie': f'sessionid={sessionid}',
            'Referer': 'https://www.trae.cn/',
            'Origin': 'https://www.trae.cn',
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/json',
        },
    )
    if status != 200:
        raise RuntimeError(f'Login 失败 (http={status})，sessionid 可能已过期')
    # 头名大小写不固定，逐个宽松匹配
    for key, value in headers.items():
        if key.lower() == 'set-cookie' and value.strip().startswith('X-Cloudide-Session='):
            return value.strip().split('=', 1)[1].split(';', 1)[0].strip()
    raise RuntimeError('Login 响应缺少 X-Cloudide-Session')


def mint_jwt(session: str) -> dict:
    """用 X-Cloudide-Session 换取 Cloud-IDE-JWT，返回 token 与过期时间"""
    status, _, text = _post(
        ROOT_URL + GET_TOKEN_PATH,
        {
            'Cookie': f'X-Cloudide-Session={session}',
            'Referer': 'https://www.trae.cn/',
            'Origin': 'https://www.trae.cn',
            'User-Agent': USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
        },
    )
    if status != 200:
        raise RuntimeError(f'GetUserToken 失败 (http={status})')
    try:
        result = (json.loads(text) or {}).get('Result') or {}
    except ValueError:
        raise RuntimeError(f'GetUserToken 返回非 JSON: {text[:200]}')
    token = result.get('Token')
    if not token:
        raise RuntimeError(f'GetUserToken 未返回 Token: {str(result)[:200]}')
    return {
        'token': token,
        'expired_at': result.get('ExpiredAt') or '',
        'user_id': str(result.get('UserID') or ''),
        'tenant_id': result.get('TenantID') or '',
    }


def push_to_relay(creds: dict, relay_url: str, api_key: str) -> dict:
    """把 JWT 写入 relay 账号池（热加载，无需重启容器）"""
    payload = {
        'token': creds['token'],
        'expiredAt': creds['expired_at'],
        'userId': creds['user_id'],
        'tenantId': creds['tenant_id'],
        'host': 'https://trae-api-cn.mchost.guru',
        'region': 'cn',
        'aiRegion': 'cn',
        'userRegion': 'CN',
        'scope': 'marscode-cn',
        'tenant': 'marscode',
        'appLanguage': 'zh-CN',
        'appVersion': '1.0.0.1229',
        'label': f"trae-cn-{creds['user_id'] or 'default'}",
    }
    headers = {'Content-Type': 'application/json'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    status, _, text = _post(f'{relay_url.rstrip("/")}/api/web-auth',
                            headers, json.dumps(payload).encode('utf-8'))
    if status != 200:
        raise RuntimeError(f'relay /api/web-auth 失败 (http={status}): {text[:200]}')
    try:
        body = json.loads(text) or {}
    except ValueError:
        raise RuntimeError(f'relay 返回非 JSON: {text[:200]}')
    if not body.get('success'):
        raise RuntimeError(f'relay 拒绝写入: {str(body)[:200]}')
    return body


def load_relay_settings() -> tuple:
    """从 .env 读取 relay 地址与 API key；环境变量优先（容器内可覆盖）"""
    relay_url = os.environ.get('RELAY_PUBLIC_URL') or DEFAULT_RELAY_URL
    api_key = os.environ.get('RELAY_API_KEY') or ''
    if RELAY_ENV.is_file():
        for line in RELAY_ENV.read_text('utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == 'RELAY_PUBLIC_URL' and value and not os.environ.get('RELAY_PUBLIC_URL'):
                relay_url = value
            elif key == 'RELAY_API_KEYS' and value and not api_key:
                api_key = value.split(',')[0].strip()
    return relay_url, api_key


def main() -> int:
    parser = argparse.ArgumentParser(description='Trae CN JWT 供给脚本')
    parser.add_argument('--dry-run', action='store_true', help='只换 JWT，不写入 relay')
    parser.add_argument('--quiet', action='store_true', help='静默模式（正常时只输出一行）')
    parser.add_argument('--relay-url', help='覆盖 relay 地址')
    parser.add_argument('--token-json', help='覆盖 config/token.json 路径')
    args = parser.parse_args()

    try:
        token_json = Path(args.token_json) if args.token_json else TOKEN_JSON
        sessionid = read_sessionid(token_json)
        log('[1/3] 用 sessionid 刷新 X-Cloudide-Session ...', args.quiet)
        session = refresh_cloudide_session(sessionid)
        log(f'      成功（session 长度 {len(session)}）', args.quiet)

        log('[2/3] 换取 Cloud-IDE-JWT ...', args.quiet)
        creds = mint_jwt(session)
        log(f"      user_id={creds['user_id']} expired_at={creds['expired_at']}", args.quiet)

        if args.dry_run:
            log('[3/3] dry-run，跳过写入 relay', args.quiet)
            return 0

        relay_url, api_key = load_relay_settings()
        if args.relay_url:
            relay_url = args.relay_url
        log(f'[3/3] 写入 relay {relay_url} ...', args.quiet)
        body = push_to_relay(creds, relay_url, api_key)
        if args.quiet:
            print(f"[OK] user={creds['user_id']} expires={creds['expired_at']}", flush=True)
        else:
            log(f"      成功 has_token={body.get('has_token')} user_id={body.get('user_id')}", args.quiet)
        return 0
    except Exception as exc:  # noqa: BLE001 - 定时任务需要统一出口
        return fail(str(exc))


if __name__ == '__main__':
    sys.exit(main())
