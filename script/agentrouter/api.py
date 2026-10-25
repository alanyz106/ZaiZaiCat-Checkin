#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Agent Router（ps.air-outer.com，原域名 agentrouter.org）签到 API 模块

站点基于 New API，签到规则特殊：控制台没有「签到」按钮，额度在**登录流程中**由服务端
发放（官方 FAQ：「签到领 $25 额度」需要退出后重新登录才会到账）。因此本模块的签到动作
就是完整重走一次 GitHub OAuth 登录，签到结果看登录响应的 `checked_in` 字段。

登录链路（复刻前端 /assets/index-*.js 的实现）：
    1. GET {base}/api/oauth/state
       返回签名 state，同时下发 session cookie（服务端校验 state 依赖该 cookie）
    2. GET github.com/login/oauth/authorize?client_id=&state=&scope=user:email
       携带 github.com 登录态（由配置提供）后 302 到回调地址并附带 code。
       回调域名由 GitHub OAuth App 注册信息决定，通常是 https://agentrouter.org
       （境内不可达），脚本只从 Location 中取 code、从不访问该域名，随后统一用
       base_url 发起回调请求，等价于把回调域名替换为 ps.air-outer.com。
       若该 App 尚未授权，GitHub 会返回授权页面，脚本自动提交同意表单完成首次授权。
    3. GET {base}/api/oauth/github?code=&state=&mode=login
       登录成功即完成签到，响应 data 为用户对象，其中 checked_in 为签到标记
"""

import base64
import html
import logging
import os
import re
import ssl
import tempfile
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import certifi
import requests

logger = logging.getLogger(__name__)

# 备用域名（原域名 agentrouter.org 境内不通，默认走备用域名）
DEFAULT_BASE_URL = 'https://ps.air-outer.com'
# /api/status 会下发 github_client_id，取不到时的兜底值
FALLBACK_CLIENT_ID = 'Ov23lidtiR4LeVZvVRNL'
GITHUB_HOST = 'https://github.com'
GITHUB_COOKIE_DOMAIN = '.github.com'

DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)

# 换算额度为美元的默认值（站点 /api/status 的 quota_per_unit）
DEFAULT_QUOTA_PER_UNIT = 500000

# 经代理访问时链路偶发抖动，/api/oauth/state 的有限重试次数与递增间隔（秒）
OAUTH_STATE_RETRY = 3
OAUTH_STATE_RETRY_DELAY = 2.0


def build_github_cookies(session_value: str = '') -> Dict[str, str]:
    """
    由 user_session 值构造请求 github.com 使用的 cookie 字典

    GitHub 校验登录态时同时参考 logged_in / __Host-user_session_same_site
    两个标记，脚本据此自动补齐，配置只需提供 user_session 一个值。

    Args:
        session_value (str): github.com 的 user_session cookie 值

    Returns:
        Dict[str, str]: cookie 名值对
    """
    cookies: Dict[str, str] = {}
    session_value = (session_value or '').strip()
    if session_value:
        cookies['user_session'] = session_value
        cookies.setdefault('logged_in', 'yes')
        cookies.setdefault('__Host-user_session_same_site', session_value)
    return cookies


def _unescape(text: str) -> str:
    return html.unescape(text or '')


def _resolve_proxy(proxy: str = '') -> Optional[Dict[str, str]]:
    """
    解析代理配置：显式传入的 proxy 优先，其次读环境变量。

    站点 ps.air-outer.com 前置了阿里云 WAF，机房/云服务器公网 IP 常被直接拦截
    （返回 200 + WAF 挑战页，正文不是 JSON）。此时必须经代理出口访问。

    取证顺序：proxy 参数 → HTTPS_PROXY → https_proxy → HTTP_PROXY → http_proxy
    → ALL_PROXY → all_proxy。全部为空则返回 None（直连）。

    Args:
        proxy (str): 配置中的 proxy 字段，如 http://127.0.0.1:7890

    Returns:
        Optional[Dict[str, str]]: requests 可直接使用的 proxies 字典，或 None
    """
    candidate = (proxy or '').strip()
    if not candidate:
        for key in ('HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy',
                    'ALL_PROXY', 'all_proxy'):
            value = (os.environ.get(key) or '').strip()
            if value:
                candidate = value
                break
    if not candidate:
        return None
    return {'http': candidate, 'https': candidate}


@lru_cache(maxsize=1)
def build_ca_bundle() -> Optional[str]:
    """
    合并 certifi 与 Windows 系统根证书，返回 PEM 文件路径

    本机常见的 GitHub 加速器（SteamTools / Watt Toolkit 等）会对 github.com 做本地
    TLS 中间人，其根证书只装在系统证书库中、不在 certifi 里，直接请求会报
    「certificate verify failed: unable to get local issuer certificate」。
    合并两者后 requests 才能正常校验。非 Windows 或导出失败时返回 None（沿用 certifi）。
    """
    parts = []
    try:
        parts.append(Path(certifi.where()).read_text(encoding='ascii'))
    except Exception as exc:
        logger.debug(f'读取 certifi 证书失败: {exc}')

    try:
        for der, encoding, _ in ssl.enum_certificates('ROOT'):
            if encoding != 'x509_asn':
                continue
            encoded = base64.b64encode(der).decode('ascii')
            body = '\n'.join(encoded[i:i + 64] for i in range(0, len(encoded), 64))
            parts.append(f'-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----\n')
    except Exception as exc:
        logger.debug(f'导出系统根证书失败: {exc}')

    if not parts:
        return None

    try:
        # 唯一临时文件（mkstemp 默认 0600 权限），避免可预测路径被劫持与普通覆盖竞态
        fd, path = tempfile.mkstemp(prefix='agentrouter_ca_bundle_', suffix='.pem')
        os.close(fd)
        Path(path).write_text('\n'.join(parts), encoding='ascii')
        return path
    except Exception as exc:
        logger.debug(f'写入 CA 合并文件失败: {exc}')
        return None


class AgentRouterAPI:
    """Agent Router 登录签到 API 类"""

    def __init__(self,
                 base_url: str = '',
                 github_session: str = '',
                 user_id: str = '',
                 proxy: str = '',
                 verify: Any = None,
                 timeout: int = 30):
        """
        Args:
            base_url (str): 站点地址，默认 https://ps.air-outer.com
            github_session (str): github.com 的 user_session 值
            user_id (str): 站点用户 ID，用于 new-api-user 请求头（登录响应自动获取）
            proxy (str): 代理地址，如 http://127.0.0.1:7890；留空则沿用环境变量
            verify (Any): TLS 校验，留空自动合并系统根证书；可填证书路径或 False
            timeout (int): 请求超时时间（秒）
        """
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip('/')
        self.github_cookies = build_github_cookies(github_session)
        self.user_id = str(user_id or '').strip()
        self.timeout = timeout
        self.verify = verify
        # 显式 proxy 优先；未配置时回退到环境变量（HTTP(S)_PROXY / ALL_PROXY）
        self.proxies = _resolve_proxy(proxy)
        self._client_id: Optional[str] = None
        self._quota_per_unit = DEFAULT_QUOTA_PER_UNIT

    # ------------------------------------------------------------------ 基础请求

    def _prepare(self, session: requests.Session) -> requests.Session:
        """统一设置 TLS 校验与代理"""
        if self.verify is not None:
            session.verify = self.verify
        else:
            ca_bundle = build_ca_bundle()
            if ca_bundle:
                session.verify = ca_bundle
        if self.proxies:
            session.proxies.update(self.proxies)
        return session

    def _new_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            'User-Agent': DEFAULT_USER_AGENT,
            'Accept': 'application/json, text/plain, */*',
        })
        # 站点用 new-api-user 请求头识别用户，缺它会返回「未提供 New-Api-User」
        if self.user_id:
            session.headers['new-api-user'] = self.user_id
        return self._prepare(session)

    @staticmethod
    def _json_of(response: requests.Response) -> Dict[str, Any]:
        try:
            body = response.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    @staticmethod
    def _extract_user_id(data: Any) -> Optional[str]:
        """从登录响应的用户对象中提取用户 ID"""
        if isinstance(data, dict):
            for key in ('id', 'user_id', 'uid'):
                if data.get(key):
                    return str(data[key])
        return None

    # ------------------------------------------------------------------ 站点信息

    def get_status(self) -> Dict[str, Any]:
        """获取站点公开配置（含 github_client_id、额度换算比例）"""
        try:
            resp = self._new_session().get(f'{self.base_url}/api/status', timeout=self.timeout)
            body = self._json_of(resp)
            data = body.get('data') or {}
            if data.get('github_client_id'):
                self._client_id = data['github_client_id']
            quota_per_unit = data.get('quota_per_unit')
            if isinstance(quota_per_unit, (int, float)) and quota_per_unit > 0:
                self._quota_per_unit = quota_per_unit
            return {'success': bool(body.get('success')), 'data': data}
        except Exception as exc:
            logger.warning(f'获取站点状态失败: {exc}')
            return {'success': False, 'data': {}}

    def get_github_client_id(self) -> str:
        """获取 GitHub OAuth client_id，失败时回退到已知值"""
        if not self._client_id:
            self.get_status()
        return self._client_id or FALLBACK_CLIENT_ID

    # ------------------------------------------------------------------ OAuth 登录

    def fetch_oauth_state(self, session: requests.Session) -> str:
        """
        获取登录用 state（服务端会把它写入本次会话）

        经代理访问时链路偶发抖动（连接被重置/超时/WAF 挑战页），这里做有限次重试；
        该接口是只读的，重试安全。

        Returns:
            str: 签名 state

        Raises:
            RuntimeError: 接口未返回 state
        """
        last_error = ''
        for attempt in range(OAUTH_STATE_RETRY):
            try:
                resp = session.get(f'{self.base_url}/api/oauth/state', timeout=self.timeout)
                if resp.status_code == 429:
                    # 站点限流：按 Retry-After 等待，避免频繁重试把限流窗口越拉越长
                    retry_after = resp.headers.get('Retry-After') or ''
                    try:
                        wait = max(float(retry_after), OAUTH_STATE_RETRY_DELAY)
                    except (TypeError, ValueError):
                        wait = OAUTH_STATE_RETRY_DELAY * (attempt + 1)
                    last_error = '站点限流 (HTTP 429)，请求过于频繁'
                    if attempt < OAUTH_STATE_RETRY - 1:
                        time.sleep(min(wait, 60))
                    continue
                body = self._json_of(resp)
                state = body.get('data')
                if body.get('success') and state:
                    return str(state)
                last_error = body.get('message') or resp.text[:200]
            except requests.RequestException as exc:
                last_error = f'{exc.__class__.__name__}: {exc}'
            if attempt < OAUTH_STATE_RETRY - 1:
                time.sleep(OAUTH_STATE_RETRY_DELAY * (attempt + 1))
        raise RuntimeError(f'获取 oauth state 失败: {last_error}')

    def _github_session(self) -> requests.Session:
        session = requests.Session()
        session.headers.update({
            'User-Agent': DEFAULT_USER_AGENT,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        })
        for name, value in self.github_cookies.items():
            session.cookies.set(name, value, domain=GITHUB_COOKIE_DOMAIN, path='/')
        return self._prepare(session)

    @staticmethod
    def _extract_code(response: requests.Response) -> Optional[str]:
        """从 GitHub 的 302 Location 中提取授权码 code"""
        location = response.headers.get('Location') or ''
        if not location or 'code=' not in location:
            return None
        # 跳回 github.com/login 说明登录态无效
        if location.startswith(f'{GITHUB_HOST}/login'):
            return None
        return parse_qs(urlparse(location).query).get('code', [None])[0]

    def _approve_authorize(self, github: requests.Session, page_html: str,
                           authorize_url: str) -> Optional[str]:
        """
        首次授权时自动提交 GitHub 的「Authorize」同意表单

        Args:
            github (requests.Session): 带 GitHub 登录态的会话
            page_html (str): 授权页面 HTML
            authorize_url (str): 授权页地址，用于 Referer

        Returns:
            Optional[str]: 授权码 code，失败返回 None
        """
        action_match = re.search(r'<form[^>]+action="([^"]+)"', page_html)
        if not action_match:
            return None

        form_action = _unescape(action_match.group(1))
        if not form_action.startswith('http'):
            form_action = GITHUB_HOST + form_action

        payload: Dict[str, str] = {}
        for tag in re.findall(r'<input[^>]*>', page_html):
            if 'type="hidden"' not in tag:
                continue
            name_match = re.search(r'name="([^"]+)"', tag)
            value_match = re.search(r'value="([^"]*)"', tag)
            if name_match:
                payload[name_match.group(1)] = _unescape(value_match.group(1)) if value_match else ''
        if not payload:
            return None

        # 同意按钮：name="authorize" value="1"
        payload['authorize'] = '1'
        resp = github.post(form_action, data=payload, timeout=self.timeout,
                           allow_redirects=False, headers={'Referer': authorize_url})
        if resp.status_code not in (301, 302, 303):
            logger.debug(f'提交授权表单后状态码异常: {resp.status_code}')
        return self._extract_code(resp)

    def authorize_github(self, state: str, client_id: str = '') -> Tuple[Optional[str], str]:
        """
        用 GitHub 登录态换取授权码

        Args:
            state (str): /api/oauth/state 下发的 state
            client_id (str): GitHub OAuth client_id，留空则从站点状态接口获取

        Returns:
            Tuple[Optional[str], str]: (授权码 code, 方式说明)
        """
        client_id = client_id or self.get_github_client_id()
        github = self._github_session()
        params = {'client_id': client_id, 'state': state, 'scope': 'user:email'}
        authorize_url = f'{GITHUB_HOST}/login/oauth/authorize'

        resp = github.get(authorize_url, params=params, timeout=self.timeout, allow_redirects=False)
        logger.debug(f'GitHub 授权响应: {resp.status_code} -> {resp.headers.get("Location", "")[:120]}')

        code = self._extract_code(resp)
        if code:
            return code, '已授权应用，静默换取授权码'

        # 429：GitHub 对未认证/高频请求的限流，不代表登录态失效，必须区分开来
        if resp.status_code == 429:
            raise RuntimeError(
                'GitHub 接口限流 (HTTP 429)，本机或代理出口请求过于频繁，请稍后重试；'
                '这不是登录态失效，无需重新获取 Cookie')

        if resp.status_code in (301, 302, 303):
            location = resp.headers.get('Location') or ''
            if location.startswith(f'{GITHUB_HOST}/login'):
                raise RuntimeError(
                    'GitHub 登录态无效或已过期，请更新配置中的 github_session')
            raise RuntimeError(f'GitHub 授权未返回 code，跳转地址: {location[:200]}')

        # 200：首次授权需要确认，自动提交同意表单
        code = self._approve_authorize(github, resp.text, resp.url)
        if code:
            return code, '首次授权，已自动提交同意表单'

        if '/login' in resp.url or 'Sign in to GitHub' in resp.text:
            raise RuntimeError(
                'GitHub 登录态无效或已过期，请更新配置中的 github_session')
        raise RuntimeError(
            '未能从 GitHub 获取授权码，请在浏览器中手动授权一次该应用后重试，'
            f'或检查账号是否开启了额外验证。授权地址: {resp.url[:200]}')

    def login_with_code(self, session: requests.Session, code: str, state: str) -> Dict[str, Any]:
        """
        用授权码完成登录（服务端在此完成签到）

        Returns:
            Dict[str, Any]: 含 success/message/data 的登录结果
        """
        resp = session.get(f'{self.base_url}/api/oauth/github',
                           params={'code': code, 'state': state, 'mode': 'login'},
                           timeout=self.timeout)
        body = self._json_of(resp)
        message = str(body.get('message') or '')
        if not body.get('success'):
            return {'success': False, 'error': message or f'登录失败（HTTP {resp.status_code}）'}
        return {'success': True, 'message': message, 'data': body.get('data')}

    def login_by_github(self) -> Dict[str, Any]:
        """
        完整走一遍 GitHub OAuth 登录（即签到动作）

        关键：state 会话与回调登录**必须复用同一个 session**。
        /api/oauth/state 会把 state 绑定到本次会话下发的 cookie 上，回调
        /api/oauth/github 校验 state 依赖该 cookie；若中途换新 session，
        服务端会返回 success=false（且 message 为空），表现为「登录失败」。
        GitHub 会话与站点会话相互独立，只携带站点 cookie，不会泄露到 GitHub。

        Returns:
            Dict[str, Any]: 含 success/session/mode/user/checked_in 的结果；
                失败时含 error/error_type
        """
        if not self.github_cookies:
            return {
                'success': False,
                'error': '缺少 GitHub 登录态，请在配置中填写 github_session',
                'error_type': 'missing_credential',
            }

        session = self._new_session()
        try:
            state = self.fetch_oauth_state(session)
            code, via = self.authorize_github(state)
            if not code:
                return {'success': False, 'error': 'GitHub 未返回授权码', 'error_type': 'auth_failed'}
            # 复用 session（含 state 对应的站点 cookie），不能新建会话
            result = self.login_with_code(session, code, state)
        except Exception as exc:
            return {'success': False, 'error': str(exc), 'error_type': 'auth_failed'}

        if not result['success']:
            result['error_type'] = 'login_failed'
            return result

        return self._finalize_login(session, result.get('data'), f'GitHub OAuth 登录（{via}）')

    def _finalize_login(self, session: requests.Session, data: Any, mode: str) -> Dict[str, Any]:
        """
        收尾登录结果：补 new-api-user 头、提取用户信息与签到标记

        服务端在登录流程中完成签到，响应的 data 即用户对象，`checked_in` 表示
        本次登录是否达成签到（前端据此弹出「签到成功，新增额度已到账」）。
        """
        user = data if isinstance(data, dict) else {}
        user_id = self._extract_user_id(data)
        if user_id:
            self.user_id = user_id
            session.headers['new-api-user'] = user_id

        return {
            'success': True,
            'session': session,
            'mode': mode,
            'user': user,
            'user_id': user_id,
            'checked_in': bool(user.get('checked_in')),
        }

    # ------------------------------------------------------------------ 账号信息

    def get_user_self(self, session: requests.Session) -> Dict[str, Any]:
        """
        查询当前登录用户信息（含额度）

        Returns:
            Dict[str, Any]: 含 success/user 的结果，user 为接口 data 字段
        """
        try:
            resp = session.get(f'{self.base_url}/api/user/self', timeout=self.timeout)
            body = self._json_of(resp)
        except Exception as exc:
            return {'success': False, 'error': f'查询用户信息失败: {exc}'}

        message = str(body.get('message') or '')
        if not body.get('success'):
            if resp.status_code == 401 or 'New-Api-User' in message:
                return {
                    'success': False,
                    'error': '登录态无效，或缺少用户 ID（请在账号配置中填写 user_id）',
                }
            return {'success': False, 'error': message or '查询用户信息失败'}
        return {'success': True, 'user': body.get('data') or {}}

    def quota_to_usd(self, quota: Any) -> float:
        """把站点额度换算为美元（换算比例取自 /api/status 的 quota_per_unit）"""
        try:
            return round(float(quota) / self._quota_per_unit, 2)
        except (TypeError, ValueError):
            return 0.0
