#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
余额查询模块（供 run_all.py 汇总推送使用）

各平台在「签到」之外还关心「还剩多少额度能用」。本模块把余额查询独立出来，
与签到主流程解耦：**任何异常都只返回 None，绝不影响签到结果与推送**。

已支持（2026-10-25 实测）：

| 平台 | 接口 | 口径 |
|---|---|---|
| trae | `POST https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage` | 账号总积分账户 |
| workbuddy | `POST https://www.codebuddy.cn/v2/billing/meter/get-user-resource` | 资源包剩余合计（**多账号逐个统计**，2026-10-29） |
| huaruntong_wx | `/api/points/querySummary` | 华润通可用积分 |
| modelscope | `GET https://modelscope.cn/openapi/v1/magicubes/balance` | 魔搭魔粒可用余额（2026-10-30） |

凭证复用 `config/token.json`，与签到脚本同一份，不额外配置。

多账号口径（2026-10-29）
-----------------------
`fetch_workbuddy_balance` 遍历 `workbuddy.accounts` 的**全部**账号，返回体除汇总
字段外还带 `accounts` 列表。`format_balance_line` 在账号数 > 1 时改为分条展示：

    💰 alan 3943.89（至 2026-11-19） | FantaSea 596.41（至 2026-10-31） ｜ 合计 4540.30

单账号（或旧调用方没传 `accounts`）时退化为原来的单行格式，行为不变。
单个账号查询失败只跳过它自己，不影响其它账号与汇总推送。

设计要点
--------
1. **只读**：本模块不写任何配置文件。
2. **独立超时**：单项余额查询超时远小于签到，避免拖慢整体调度。
3. **失败降级**：拿不到就返回 None，汇总里不显示该项，不报错。
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
TOKEN_PATH = PROJECT_ROOT / "config" / "token.json"
DEFAULT_TIMEOUT = 15


def _load_config() -> Dict[str, Any]:
    """读取 config/token.json，失败返回空字典"""
    try:
        if not TOKEN_PATH.is_file():
            return {}
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _load_platform_api(platform: str):
    """
    按平台加载各自的 api.py

    ⚠️ 关键坑：`script/trae/api.py` 与 `script/workbuddy/api.py` **同名**，
    都用 `sys.path.insert` + `import api` 的话，后加载的一方会命中
    sys.modules 里已缓存的另一方（实测报 `cannot import name 'WorkBuddyAPI'
    from 'api'`）。故用 importlib 以带平台前缀的唯一模块名加载，互不干扰。
    """
    import importlib.util

    mod_name = f"_checkin_{platform}_api"
    if mod_name in sys.modules:
        return sys.modules[mod_name]

    path = PROJECT_ROOT / "script" / platform / "api.py"
    if not path.is_file():
        raise ImportError(f"找不到 {path}")

    spec = importlib.util.spec_from_file_location(mod_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载 {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


def _num(value: Any) -> Optional[float]:
    """宽松转浮点，失败返回 None（上游有的给字符串有的给数字）"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _fmt(value: float) -> str:
    """整数不带小数点，小数保留两位"""
    return str(int(value)) if abs(value - round(value)) < 1e-9 else f"{value:.2f}"


# ---------------------------------------------------------------------------
# 各平台余额查询
# ---------------------------------------------------------------------------

def fetch_trae_balance(timeout: int = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """
    查询 Trae CN 账号积分余额

    链路与签到脚本一致：sessionid -> X-Cloudide-Session -> Cloud-IDE-JWT -> 余额接口。
    必须换 JWT：余额接口不接受 sessionid。

    Returns:
        Optional[Dict]: {'used','total','remaining','packs','expire_at'}；失败返回 None
    """
    config = _load_config()
    accounts = (config.get("trae") or {}).get("accounts") or []
    sessionid = next(
        (str(a.get("sessionid") or "").strip()
         for a in accounts if a.get("sessionid")),
        "",
    )
    if not sessionid:
        return None

    try:
        api_mod = _load_platform_api("trae")
        TraeWebAPI = api_mod.TraeWebAPI
        login_refresh = api_mod.login_refresh

        refreshed = login_refresh(sessionid, timeout=timeout)
        if not refreshed.get("success"):
            logging.debug("trae 余额: 刷新 session 失败 %s",
                          refreshed.get("error"))
            return None

        api = TraeWebAPI(refreshed["session"], timeout=timeout)
        if not api.get_user_id():
            logging.debug("trae 余额: 换 JWT 失败")
            return None

        import requests

        resp = requests.post(
            "https://api.trae.cn/trae/api/v2/pay/ide_user_ent_usage",
            headers={
                "Authorization": f"Cloud-IDE-JWT {api._jwt}",
                "Content-Type": "application/json",
                "x-device-id": "1234567890123456",
                "User-Agent": "VSCode 1.107.1 (TRAE SOLO CN)",
            },
            json={"require_usage": True, "req_source": 1},
            timeout=timeout,
        )
        if resp.status_code != 200:
            logging.debug("trae 余额: http=%s", resp.status_code)
            return None
        data = resp.json() or {}
        if data.get("code") not in (None, 0):
            logging.debug("trae 余额: 业务错误 %s", data.get("message"))
            return None

        summary = data.get("usage_summary") or {}
        total = _num(summary.get("total_amount"))
        used = _num(summary.get("consumed_amount"))
        if total is None:
            return None
        remaining = round(total - (used or 0), 2)

        # 多个资源包各有到期时间，取最晚的一个作为整体有效期
        expire_at = ""
        for pack in data.get("user_entitlement_pack_list") or []:
            end = (pack.get("expire_time")
                   or (pack.get("entitlement_base_info") or {}).get("end_time"))
            if isinstance(end, (int, float)) and end > 0:
                stamp = time.strftime("%Y-%m-%d", time.localtime(end))
                if stamp > expire_at:
                    expire_at = stamp

        return {
            "used": used or 0,
            "total": total,
            "remaining": remaining,
            "packs": len(data.get("user_entitlement_pack_list") or []),
            "expire_at": expire_at,
        }
    except Exception as exc:  # noqa: BLE001
        logging.debug("trae 余额查询失败: %s", exc)
        return None


def _workbuddy_packs_balance(packs: list) -> Optional[Dict[str, Any]]:
    """
    把单个 WorkBuddy 账号的资源包列表折算成余额字典（纯计算，便于多账号复用）

    上游按「资源包」发放积分（活动赠送、拉新权益等各成一个包，各有独立周期），
    只统计 `Status == 0`（有效）的包。

    口径说明（2026-10-29 修正）
    --------------------------
    早期版本用 `CapacityRemainPrecise`（包总剩余）求和，会虚高。
    该字段与 `CycleCapacityRemainPrecise`（当期剩余）在「体验版」等特殊包上
    会互相矛盾：实测 `CapacityRemain=500` 而 `CycleCapacityRemain=0`、
    `CycleCapacityUsed=500`（即额度已用尽，但包级字段没同步），
    导致余额虚高 500 分，且「最近到期」提示指向一个实际剩 0 的包。

    腾讯计费资源包的正确语义是：

        可用额度 = 当期剩余 + 剩余整周期数 × 每周期额度
                 = CycleCapacityRemain + RemainCycles * CycleCapacitySize

    当前所有包均为单周期（RemainCycles=0），故等价于 CycleCapacityRemain 求和。

    Returns:
        Optional[Dict]: {'remaining','total','used','packs','usable_packs','soonest_expire'}
    """
    remaining = used = total = 0.0
    active = 0
    usable = 0          # 真实还有额度可用的包数（区别于 active）
    soonest = ""
    for pack in packs:
        # Status 0 为有效；其它值（失效/冻结）不计入可用余额
        if str(pack.get("Status")) != "0":
            continue
        active += 1

        # --- 当期剩余 ---
        cycle_remain = _num(pack.get("CycleCapacityRemainPrecise"))
        if cycle_remain is None:
            cycle_remain = _num(pack.get("CycleCapacityRemain"))

        # --- 未来整周期额度（多次续期包才有意义）---
        cycles_left = _num(pack.get("RemainCycles")) or 0
        cycle_size = _num(pack.get("CycleCapacitySizePrecise"))
        if cycle_size is None:
            cycle_size = _num(pack.get("CycleCapacitySize"))

        if cycle_remain is None:
            # 上游极少数包可能不返回 CycleCapacity*，
            # 此时才退回包级字段，至少不把整个余额算成 0
            remain = _num(pack.get("CapacityRemainPrecise"))
            if remain is None:
                remain = _num(pack.get("CapacityRemain"))
            remain = remain or 0
        else:
            remain = cycle_remain + cycles_left * (cycle_size or 0)

        # --- 总量 / 已用：用于展示「还剩多少 / 一共多少」---
        if cycle_remain is not None and cycle_size is not None:
            cycles_total = _num(pack.get("TotalCycles")) or 1
            size = cycle_size * cycles_total
            used_v = size - remain
        else:
            size = _num(pack.get("CapacitySizePrecise"))
            if size is None:
                size = _num(pack.get("CapacitySize"))
            used_v = _num(pack.get("CapacityUsedPrecise"))
            if used_v is None:
                used_v = _num(pack.get("CapacityUsed"))

        remaining += remain
        total += size or 0
        used += max(used_v or 0, 0)

        if remain > 0:
            usable += 1
            cycle_end = str(pack.get("CycleEndTime") or "")
            # 只关心「真的还有余额且最早到期」的包，用来提示何时该续
            if cycle_end and (not soonest or cycle_end < soonest):
                soonest = cycle_end

    return {
        "remaining": round(remaining, 2),
        "total": round(total, 2),
        "used": round(used, 2),
        "packs": active,
        "usable_packs": usable,
        "soonest_expire": soonest[:10] if soonest else "",
    }


def _fetch_one_workbuddy(account: Dict[str, Any], WorkBuddyAPI,
                         timeout: int) -> Optional[Dict[str, Any]]:
    """查询单个 WorkBuddy 账号的余额；失败返回 None（不影响其它账号）"""
    try:
        api = WorkBuddyAPI(
            access_token=account["access_token"],
            refresh_token=account.get("refresh_token"),
            uid=account.get("uid"),
            domain=account.get("domain"),
            timeout=timeout,
        )
        resp = api._post("/v2/billing/meter/get-user-resource")
        if not isinstance(resp, dict):
            logging.debug("workbuddy 余额: %s", resp)
            return None
        if not resp.get("success"):
            logging.debug("workbuddy 余额: %s", resp.get("error"))
            return None
        payload = resp.get("data") or resp.get("Data") or {}
        if not isinstance(payload, dict):
            logging.debug("workbuddy 余额: Response %s", payload)
            return None
        packs = payload.get("Accounts") or payload.get("accounts") or []
        if not isinstance(packs, list):
            return None
        return _workbuddy_packs_balance(packs)
    except Exception as exc:  # noqa: BLE001
        logging.debug("workbuddy 余额查询失败: %s", exc)
        return None


def fetch_workbuddy_balance(timeout: int = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """
    查询 WorkBuddy(CodeBuddy) 积分余额（**全部账号**）

    config/token.json 的 workbuddy.accounts 里可能配了多个账号。
    2026-10-29 起逐个统计并在汇总推送里分条展示——此前只取第一个有
    access_token 的账号，第二个账号（FantaSea）的余额完全看不到。

    Returns:
        Optional[Dict]: 汇总口径（各账号求和），额外带 `accounts` 列表，
        每项含 account_name 及该账号的 remaining/total/used/packs/
        usable_packs/soonest_expire。全部账号都查不到时返回 None。
    """
    config = _load_config()
    accounts = [
        a for a in ((config.get("workbuddy") or {}).get("accounts") or [])
        if a.get("access_token")
    ]
    if not accounts:
        return None

    try:
        WorkBuddyAPI = _load_platform_api("workbuddy").WorkBuddyAPI
    except Exception as exc:  # noqa: BLE001
        logging.debug("workbuddy api 加载失败: %s", exc)
        return None

    per_account: list = []
    for idx, account in enumerate(accounts):
        info = _fetch_one_workbuddy(account, WorkBuddyAPI, timeout)
        if not info:
            continue
        name = (account.get("account_name") or account.get("email")
                or account.get("uid") or f"账号{idx + 1}")
        info["account_name"] = name
        per_account.append(info)

    if not per_account:
        return None

    summed = {
        "remaining": round(sum(i["remaining"] for i in per_account), 2),
        "total": round(sum(i["total"] for i in per_account), 2),
        "used": round(sum(i["used"] for i in per_account), 2),
        "packs": sum(i["packs"] for i in per_account),
        "usable_packs": sum(i["usable_packs"] for i in per_account),
    }
    expires = [i["soonest_expire"] for i in per_account if i.get("soonest_expire")]
    summed["soonest_expire"] = min(expires) if expires else ""
    summed["accounts"] = per_account
    return summed


def fetch_huaruntong_wx_balance(timeout: int = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """
    查询华润通积分余额

    走 `/api/points/querySummary`（加密通道，复用签到脚本的 api.py，
    避免把 HMAC+AES+RSA 那套再抄一遍）。响应形如：
        {"code":"S0A00000","data":{"data":{"cPoints":{"points":644,
         "availablePoints":644,"holdPoints":0}}}}

    Returns:
        Optional[Dict]: {'remaining','total','hold'}；失败返回 None
    """
    config = _load_config()
    accounts = (config.get("huaruntong") or {}).get("huaruntong_wx", {}).get("accounts") or []
    account = next((a for a in accounts if a.get("token")), None)
    if not account:
        return None

    try:
        api_mod = _load_platform_api("huaruntong/huaruntong_wx")
        api = api_mod.HuaRunTongAPI(
            token=account["token"],
            user_agent=account.get("user_agent"),
            timeout=timeout,
        )
        resp = api.query_summary()
        if not isinstance(resp, dict) or resp.get("code") != "S0A00000":
            logging.debug("huaruntong_wx 余额: 响应异常 %s", resp)
            return None

        c_points = ((resp.get("data") or {}).get("data") or {}).get("cPoints") or {}
        available = _num(c_points.get("availablePoints"))
        total_ = _num(c_points.get("points"))
        hold = _num(c_points.get("holdPoints"))
        if available is None and total_ is None:
            return None
        return {
            "remaining": available if available is not None else total_,
            "total": total_ if total_ is not None else available,
            "hold": hold or 0,
        }
    except Exception as exc:  # noqa: BLE001
        logging.debug("huaruntong_wx 余额查询失败: %s", exc)
        return None


def fetch_modelscope_balance(timeout: int = DEFAULT_TIMEOUT) -> Optional[Dict[str, Any]]:
    """
    查询魔搭 ModelScope 魔粒余额

    走 `GET /openapi/v1/magicubes/balance`（官方 OpenAPI，bearerAuth），
    复用 `script/modelscope/api.py`。响应形如：
        {"success":true,"data":{total_balance,available_balance,frozen_amount}}

    ⚠️ 令牌门槛：魔搭令牌分「只读 / 下载 / 全部」三档，只有「全部」档能访问
    魔粒接口，其余档会被拒；此时本函数返回 None（汇总里不显示，不影响签到）。

    Returns:
        Optional[Dict]: {'remaining','total','hold'}；失败返回 None
    """
    config = _load_config()
    accounts = (config.get("modelscope") or {}).get("accounts") or []
    account = next((a for a in accounts if a.get("token")), None)
    if not account:
        return None

    try:
        api_mod = _load_platform_api("modelscope")
        api = api_mod.ModelScopeAPI(
            token=account["token"],
            site=account.get("site") or "cn",
            account_name=account.get("account_name") or "",
            proxy=account.get("proxy"),
        )
        wallet = api.balance()
        available = _num(wallet.get("available"))
        total_ = _num(wallet.get("total"))
        frozen = _num(wallet.get("frozen"))
        if available is None and total_ is None:
            return None
        return {
            "remaining": available if available is not None else total_,
            "total": total_ if total_ is not None else available,
            "hold": frozen or 0,
        }
    except Exception as exc:  # noqa: BLE001
        logging.debug("modelscope 余额查询失败: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

BALANCE_FETCHERS = {
    "trae": fetch_trae_balance,
    "workbuddy": fetch_workbuddy_balance,
    "huaruntong_wx": fetch_huaruntong_wx_balance,
    "modelscope": fetch_modelscope_balance,
}


def format_balance_line(key: str, balance: Optional[Dict[str, Any]]) -> Optional[str]:
    """
    把余额字典格式化成一行推送文案

    Returns:
        Optional[str]: 格式化后的行；无数据时返回 None（汇总里就不显示）
    """
    if not balance:
        return None
    remaining = balance.get("remaining")
    if remaining is None:
        return None

    if key == "modelscope":
        # 魔粒当日发放当日有效（24 小时过期），余额与"今日到账"是两件事，
        # 这里只报可用余额，避免误导成"攒着能用"。
        text = f"💰 魔粒 {_fmt(remaining)}"
        if balance.get("hold"):
            text += f"（冻结 {_fmt(balance['hold'])}）"
        return text

    if key == "trae":
        text = f"💰 余额 {_fmt(remaining)} / {_fmt(balance.get('total') or 0)} 积分"
        if balance.get("expire_at"):
            text += f"（有效至 {balance['expire_at']}）"
        return text

    if key == "workbuddy":
        # 多账号：分条列出各自余额，再给一行合计；
        # 单账号时退化为原来的单行格式，不引入多余的「合计」字样。
        accounts = balance.get("accounts") or []
        if len(accounts) > 1:
            parts = []
            for a in accounts:
                name = a.get("account_name") or "未命名"
                if a.get("soonest_expire"):
                    parts.append(
                        f"{name} {_fmt(a.get('remaining') or 0)}"
                        f"（至 {a['soonest_expire']}）"
                    )
                else:
                    parts.append(f"{name} {_fmt(a.get('remaining') or 0)}")
            text = "💰 " + " | ".join(parts)
            text += f" ｜ 合计 {_fmt(remaining)}"
            return text

        usable = balance.get("usable_packs")
        active = balance.get("packs", 0)
        if usable is not None and usable != active:
            packs_text = f"{usable}/{active} 个包有余额"
        else:
            packs_text = f"{active} 个有效包"
        text = f"💰 余额 {_fmt(remaining)} 积分（{packs_text}）"
        if balance.get("soonest_expire"):
            text += f"，最近到期 {balance['soonest_expire']}"
        return text

    if key == "huaruntong_wx":
        text = f"💰 可用 {_fmt(remaining)} 积分"
        if balance.get("total") is not None and _fmt(balance["total"]) != _fmt(remaining):
            text += f" / 总 {_fmt(balance['total'])}"
        if balance.get("hold"):
            text += f"（冻结 {_fmt(balance['hold'])}）"
        return text

    text = f"💰 余额 {_fmt(remaining)}"
    return text


def collect_balances(platform_keys: list) -> Dict[str, Optional[Dict[str, Any]]]:
    """
    批量收集余额，供汇总推送使用。任何异常都返回 None，不向上抛。
    """
    out: Dict[str, Optional[Dict[str, Any]]] = {}
    for key in platform_keys:
        fetcher = BALANCE_FETCHERS.get(key)
        out[key] = fetcher() if fetcher else None
    return out


if __name__ == "__main__":
    # 手动调试：python3 balance.py
    logging.basicConfig(level=logging.DEBUG, format="%(message)s")
    for k in BALANCE_FETCHERS:
        raw = BALANCE_FETCHERS[k]()
        print(f"{k}: {json.dumps(raw, ensure_ascii=False)}")
        print(f"    -> {format_balance_line(k, raw)}")
