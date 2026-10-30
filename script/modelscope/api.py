#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ModelScope（魔搭）魔粒签到 / 查询接口封装

签到机制
--------
魔搭没有独立的「签到按钮」接口。`GET /api/v1/users/login/info` 在服务端会被
计为一次登录，当天第一次调用即发放当日魔粒（登录 200 + 绑阿里云 50），
重复调用幂等、不会多发。因此「签到 = 调一次登录信息接口」。

鉴权
----
一枚 `ms-` 令牌两用：
  - 推理：`Authorization: Bearer ms-xxx`
  - 网页内部接口：`Cookie: m_session_id=ms-xxx`

但两者**不等价**（2026-10 实测）：
  - `/openapi/v1/magicubes/balance` 在官方 OpenAPI 里登记的是 bearerAuth
  - 账单 `/openapi/v1/magicubes/transactions`、用量 `/api/v1/inference/rate-limit`、
    登录信息 `/api/v1/users/login/info` 是网页内部接口，只认 Cookie
所以余额先走 Bearer，401/403 再回落 Cookie。

令牌权限
--------
魔搭建令牌时只有「只读 / 下载 / 全部」三档。**只读、下载档的令牌打魔粒类接口
会返回 403 `OperationNotAllowed`**（原文：the current token no longer supports
this operations），但打推理 `/v1/models` 仍是 200。排查时可据此区分：
令牌还有效，只是权限档位不够。

实测响应（2026-10-30）
----------------------
  balance      → {"success":true,"data":{total_balance,available_balance,frozen_amount}}
  transactions → {"success":true,"data":{page,page_size,records,total}}
                 记录字段：gmt_created / type(EARN|SPEND_CONFIRM|EXPIRE) /
                 rule_key / total_amount / title，**没有 id 字段**
  rate-limit   → {"Code":200,"Data":{"currentUsagePerDay":N}}
"""

import json
import os
import time

import requests
import urllib3

urllib3.disable_warnings()

SITES = {
    "cn": {
        "label": "国内站",
        "site": "https://modelscope.cn",
    },
    "intl": {
        "label": "国际站",
        "site": "https://www.modelscope.ai",
    },
}

LOGIN_INFO_PATH = "/api/v1/users/login/info"
BALANCE_PATH = "/openapi/v1/magicubes/balance"
TRANSACTIONS_PATH = "/openapi/v1/magicubes/transactions"
RATE_LIMIT_PATH = "/api/v1/inference/rate-limit"

# 每日到账的两条规则：登录 + 绑定阿里云
DAILY_RULES = ("daily_active", "aliyun_bindlogin")

# 账单分页：单页上限与翻页硬上限，避免异常响应导致死循环
TRANSACTIONS_PAGE_SIZE = 50
TRANSACTIONS_MAX_PAGES = 40

# 浏览器指纹。requests 默认 UA 是 python-requests/x.y，风控很容易识别。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)


class ModelScopeError(Exception):
    pass


class ModelScopeAuthError(ModelScopeError):
    """HTTP 401：令牌无效或站点不匹配。"""


class ModelScopeForbiddenError(ModelScopeError):
    """HTTP 403：令牌权限档位不够，或账号/出口被风控。"""


def log(message):
    print(str(message), flush=True)


def today_cn():
    """魔搭账单按北京时间自然日切分，不依赖宿主机时区。"""
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + 8 * 3600))


def envelope_data(payload):
    """兼容两套信封：/openapi/v1 用 data，/api/v1 用 Data。"""
    if not isinstance(payload, dict):
        return {}
    value = payload.get("Data")
    if not isinstance(value, dict):
        value = payload.get("data")
    return value if isinstance(value, dict) else {}


def envelope_message(payload):
    if not isinstance(payload, dict):
        return ""
    return str(
        payload.get("Message")
        or payload.get("message")
        or payload.get("msg")
        or ""
    ).strip()


def envelope_ok(payload):
    """没有 ModelScope 信封（代理错误页、WAF 挑战页、空响应）一律按失败处理。"""
    if not isinstance(payload, dict):
        return False
    if "Success" in payload and isinstance(payload.get("Success"), bool):
        return payload["Success"]
    if "success" in payload and isinstance(payload.get("success"), bool):
        return payload["success"]
    code = payload.get("Code", payload.get("code"))
    if code is None:
        return False
    try:
        return int(code) == 200
    except (TypeError, ValueError):
        return False


def daily_records_for_day(records, day):
    """筛出指定自然日、type=EARN 且规则属于每日发放的记录。"""
    result = []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        if str(record.get("type") or "").upper() != "EARN":
            continue
        if str(record.get("gmt_created") or "")[:10] != day:
            continue
        if str(record.get("rule_key") or "").lower() not in DAILY_RULES:
            continue
        result.append(record)
    return result


class ModelScopeAPI:
    """单个魔搭账号的接口封装。

    Args:
        token (str): ms- 开头的访问令牌
        site (str): "cn" 或 "intl"，默认 "cn"
        account_name (str): 仅用于日志
        proxy (str): 可选，HTTP(S)/SOCKS 代理
    """

    def __init__(self, token, site="cn", account_name="", proxy=None):
        self.token = (token or "").strip()
        self.site_key = site if site in SITES else "cn"
        self.config = SITES[self.site_key]
        self.account_name = account_name
        self.session = requests.Session()

        # 默认校验证书：ms- 令牌以 Cookie 明文过网，关校验等于交给中间人。
        # 只有显式 MODELSCOPE_INSECURE=1 才关闭（自签代理调试用）。
        if os.environ.get("MODELSCOPE_INSECURE", "").strip() == "1":
            self.session.verify = False
            log("⚠️ MODELSCOPE_INSECURE=1，已跳过 TLS 证书校验")

        proxy = proxy or os.environ.get("MODELSCOPE_PROXY", "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Referer": self.config["site"] + "/",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })

    # -- 底层请求 ---------------------------------------------------------

    def _request(self, path, auth):
        """auth: "bearer" 走官方 Authorization；"cookie" 走网页会话。"""
        headers = {}
        if auth == "bearer":
            headers["Authorization"] = "Bearer " + self.token
        else:
            headers["Cookie"] = "m_session_id=" + self.token
        return self.session.get(
            self.config["site"] + path, headers=headers, timeout=30)

    def get(self, path, what, auths=("cookie",)):
        """按顺序尝试鉴权方式，返回第一个非 401/403 的响应。

        余额接口在官方 OpenAPI 里登记的是 bearerAuth，网页内部接口只认 Cookie，
        用错方式会拿到 401；令牌权限档位不够则是 403。两者必须分开报，
        否则会误导用户反复重新生成令牌。
        """
        last_status = None
        last_payload = {}
        for auth in auths:
            try:
                response = self._request(path, auth)
            except Exception as exc:
                raise ModelScopeError("%s失败：%s" % (what, exc))
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            last_status = response.status_code
            last_payload = payload
            if response.status_code not in (401, 403):
                break

        if last_status == 401:
            # ⚠️ 魔搭对「令牌无效」「站点不匹配」「权限档位不足」三种情况
            # 一律回 401 InvalidAuthentication，响应体完全相同，服务端不给区分
            # 线索（实测：只读档令牌与随机假令牌在 users/me 上都是 401）。
            # 推理网关 /v1/models 不鉴权（无令牌也 200），也不能用来判断。
            # 因此这里只能把可能性一并列出，由用户自己核对。
            raise ModelScopeAuthError(
                "%s：被拒绝（HTTP 401）。可能是令牌无效/已吊销、站点不匹配"
                "（国内站令牌不能用于国际站）、或令牌权限档位不足"
                "（需「全部」档，只读/下载档无法访问魔粒接口）" % what)
        if last_status == 403:
            raise ModelScopeForbiddenError(
                "%s：被拒绝（HTTP 403）。多为令牌权限档位不足"
                "（只读/下载档不能访问魔粒接口，需用「全部」），"
                "或账号未绑阿里云/未实名、出口 IP 被风控" % what)
        if last_status < 200 or last_status >= 300:
            raise ModelScopeError("%s失败 HTTP %s" % (what, last_status))
        return last_payload

    # -- 业务接口 ---------------------------------------------------------

    def login_info(self):
        """调用即签到（服务端幂等）。"""
        return self.get(LOGIN_INFO_PATH, "每日签到")

    def balance(self):
        payload = self.get(
            BALANCE_PATH, "魔粒余额查询", auths=("bearer", "cookie"))
        if not envelope_ok(payload):
            raise ModelScopeError(
                "魔粒余额查询失败：%s"
                % (envelope_message(payload) or "上游返回失败"))
        data = envelope_data(payload)
        available = data.get("available_balance")
        if available is None:
            raise ModelScopeError("魔粒余额查询失败：响应缺少 available_balance")
        return {
            "available": available,
            "total": data.get("total_balance"),
            "frozen": data.get("frozen_amount", 0),
        }

    def transactions(self, day=None):
        """拉取魔粒账单。

        当天只要调过 API，每次调用都会写一条 SPEND_CONFIRM。只读第一页会让
        当日 EARN 行被挤出去，签到成功也被判成「未查到到账记录」，所以按页
        往下翻，直到出现早于目标日期的记录或不足一页为止。

        记录没有 id 字段，因此去重只能按整页签名比对——不能按字段做指纹，
        同一天可能有多笔金额相同的 SPEND_CONFIRM，会被误删。
        """
        collected = []
        seen_pages = set()
        for page in range(1, TRANSACTIONS_MAX_PAGES + 1):
            payload = self.get(
                "%s?page=%d&page_size=%d"
                % (TRANSACTIONS_PATH, page, TRANSACTIONS_PAGE_SIZE),
                "魔粒账单查询")
            if not envelope_ok(payload):
                raise ModelScopeError(
                    "魔粒账单查询失败：%s"
                    % (envelope_message(payload) or "上游返回失败"))
            data = envelope_data(payload)
            records = data.get("records")
            if records is None:
                records = []
            if not isinstance(records, list):
                raise ModelScopeError("魔粒账单格式异常：records 不是列表")

            # 接口若忽略 page 参数会一直返回同一页，整页签名相同即视为到底
            signature = json.dumps(records, sort_keys=True, ensure_ascii=False)
            if signature in seen_pages:
                break
            seen_pages.add(signature)

            collected.extend(records)
            if len(records) < TRANSACTIONS_PAGE_SIZE:
                break
            if day and records:
                oldest = str(
                    (records[-1] or {}).get("gmt_created") or "")[:10]
                if oldest and oldest < day:
                    break
        return collected

    def daily_earn(self, day=None):
        day = day or today_cn()
        return daily_records_for_day(self.transactions(day=day), day)

    def used_today(self):
        try:
            payload = self.get(RATE_LIMIT_PATH, "今日用量查询")
            if not envelope_ok(payload):
                return None
            return envelope_data(payload).get("currentUsagePerDay")
        except Exception:
            return None

    # -- 对外主流程 -------------------------------------------------------

    def checkin(self):
        """执行签到并回读账单确认到账。

        Returns:
            dict: {
                "ok": bool,          是否确认到位
                "claimed": bool,     True=本次新到账，False=今日此前已领
                "credit": float,     今日到账魔粒合计
                "titles": list,      到账明细（注册并登录 / 绑定阿里云账号）
                "message": str,      一句话结论
                "balance": dict|None,
                "used": int|None,
            }
        """
        day = today_cn()
        before = []
        before_error = ""
        try:
            before = self.daily_earn(day)
        except Exception as exc:
            before_error = str(exc)[:120]

        payload = self.login_info()
        if not envelope_ok(payload):
            raise ModelScopeError(
                "每日签到失败：%s"
                % (envelope_message(payload) or "上游返回失败"))

        result = {
            "ok": True,
            "claimed": False,
            "credit": 0.0,
            "titles": [],
            "message": "",
            "balance": None,
            "used": None,
        }

        if self.site_key == "cn":
            daily = list(before)
            after_error = ""
            if not daily:
                try:
                    daily = self.daily_earn(day)
                    if not daily:
                        # 账单可能异步写入，按配置的间隔复查一次
                        try:
                            delay = max(0, int(
                                os.environ.get("MODELSCOPE_VERIFY_DELAY", "2")))
                        except ValueError:
                            delay = 2
                        if delay:
                            time.sleep(delay)
                        daily = self.daily_earn(day)
                except Exception as exc:
                    after_error = str(exc)[:120]

            if daily:
                credit = sum(
                    float(r.get("total_amount") or 0)
                    for r in daily if isinstance(r, dict))
                titles = [
                    str(r.get("title") or r.get("rule_key") or "").strip()
                    for r in daily if isinstance(r, dict)]
                result["credit"] = credit
                result["titles"] = [t for t in titles if t]
                result["claimed"] = not before
                result["message"] = (
                    "签到成功，到账 %s 魔粒" % _num(credit)
                    if not before else
                    "今日已签到，到账 %s 魔粒" % _num(credit))
            else:
                # 登录已触达但账单里没有当日发放记录：不冒充成功
                result["ok"] = False
                why = after_error or before_error
                result["message"] = (
                    "登录已触达，但今日未查到到账记录"
                    + ("（账单查询失败：%s）" % why if why else
                       "（账号可能不在活动范围）"))
        else:
            # 国际站没有账单协议，只能以登录触发为准
            result["ok"] = True
            result["claimed"] = True
            result["message"] = "签到成功（国际站无账单回读）"

        try:
            result["balance"] = self.balance()
        except Exception as exc:
            result["error_balance"] = str(exc)[:100]

        result["used"] = self.used_today()
        return result

    def preview(self):
        """只读预览：不调用 login/info，因此不触发签到。

        用于先验证令牌是否可用、站点是否配对、账单能否读通。
        """
        result = {
            "ok": True,
            "claimed": False,
            "credit": 0.0,
            "titles": [],
            "message": "",
            "balance": None,
            "used": None,
        }
        if self.site_key == "cn":
            try:
                daily = self.daily_earn()
            except Exception as exc:
                result["ok"] = False
                result["message"] = "今日账单读取失败：%s" % str(exc)[:120]
            else:
                if daily:
                    credit = sum(
                        float(r.get("total_amount") or 0)
                        for r in daily if isinstance(r, dict))
                    result["credit"] = credit
                    result["claimed"] = True
                    result["message"] = "今日已签到，到账 %s 魔粒" % _num(credit)
                else:
                    result["message"] = "今日尚未签到"
        else:
            result["message"] = "国际站无账单协议，只读模式无法确认到账"

        try:
            result["balance"] = self.balance()
        except Exception as exc:
            result["ok"] = False
            result["error_balance"] = str(exc)[:100]
        result["used"] = self.used_today()
        return result


def _num(value):
    """250.0 → 250，250.5 → 250.5"""
    if value is None:
        return "0"
    return int(value) if float(value).is_integer() else value
