#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new Env('魔搭 ModelScope 魔粒签到');
cron: 10 9 * * *

魔搭社区每日魔粒签到

魔粒是魔搭 API-Inference 的免费计费单位：每天登录发 200，绑定阿里云再发 50，
共 250/天，**24 小时过期、不累积**。魔搭没有签到按钮，调用一次登录信息接口
`GET /api/v1/users/login/info` 即完成当天签到（服务端幂等）。

签到结果不只看登录接口是否成功，而是回读魔粒账单
`/openapi/v1/magicubes/transactions`，确认当日确有
`type=EARN` 且 `rule_key=daily_active / aliyun_bindlogin` 的记录才算到位，
避免把「登录成功」冒充成「签到成功」。

主要能力：
- 国内站 / 国际站分站点配置，令牌互不通用
- 多账号，账号间随机延迟（默认 8~20 秒，避免批量指纹）
- 令牌权限档位区分报错：403 直接提示「可能用了只读/下载档令牌」
- 余额、今日用量一并回读
- 支持 --dry-run：只读预览，不触发签到
- 随机启动延迟 MODELSCOPE_JITTER_MAX（默认 0，由 run_all.py 统一控节奏）

Author: Assistant
Date: 2026-10-30
"""

import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

# 项目根目录：script/modelscope/main.py -> 上溯三级
current_dir = Path(__file__).resolve().parent
project_root = current_dir.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(current_dir))

from api import ModelScopeAPI, ModelScopeAuthError, ModelScopeForbiddenError
from notification import send_notification, NotificationSound

# 启动随机延迟上限（秒）。run_all.py 会置 0，避免串行执行被拖慢。
JITTER_MAX_SECONDS = int(os.environ.get("MODELSCOPE_JITTER_MAX", "0"))

# 账号间延迟区间（秒）。社区实测建议 60~180，这里取保守下限区间的随机值，
# 单账号时不会触发。
ACCOUNT_DELAY_RANGE = (8, 20)


def load_config():
    """读取统一的 config/token.json"""
    config_path = project_root / "config" / "token.json"
    with open(config_path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def pick_accounts(config):
    """取出 modelscope 节点下的账号列表，按站点分组。

    支持在账号里用 site 字段区分站点（默认 cn）：
        {"account_name": "主号", "token": "ms-xxx", "site": "cn"}
    """
    node = config.get("modelscope") or {}
    accounts = node.get("accounts") or []
    valid = []
    for account in accounts:
        if not isinstance(account, dict):
            continue
        token = str(account.get("token") or "").strip()
        if not token:
            continue
        valid.append(account)
    return valid


def process_account(account, dry_run=False):
    """处理单个账号，返回统一结构的结果。"""
    name = str(account.get("account_name") or "未命名账号")
    token = str(account.get("token") or "").strip()
    site = str(account.get("site") or "cn").strip().lower()
    proxy = account.get("proxy")

    print("=" * 50)
    print(f"账号: {name}（站点: {'国内站' if site != 'intl' else '国际站'}"
          f"{'，只读预览' if dry_run else ''}）")
    print("=" * 50)

    result = {
        "account_name": name,
        "success": False,
        "claimed": False,
        "credit": 0.0,
        "message": "",
        "error": None,
        "needs_token_fix": False,
    }

    try:
        api = ModelScopeAPI(token=token, site=site,
                            account_name=name, proxy=proxy)
        data = api.preview() if dry_run else api.checkin()
    except ModelScopeAuthError as exc:
        result["error"] = str(exc)
        result["needs_token_fix"] = True
        print(f"❌ {exc}")
        return _report(result)
    except ModelScopeForbiddenError as exc:
        result["error"] = str(exc)
        result["needs_token_fix"] = True
        print(f"❌ {exc}")
        return _report(result)
    except Exception as exc:  # noqa: BLE001 单账号异常不拖垮整批
        result["error"] = f"{exc.__class__.__name__}: {exc}"
        print(f"❌ 处理失败: {result['error']}")
        return _report(result)

    result["success"] = bool(data.get("ok"))
    result["claimed"] = bool(data.get("claimed"))
    result["credit"] = float(data.get("credit") or 0)
    result["message"] = data.get("message") or ""

    if result["success"]:
        print(f"✅ {result['message']}")
        if result.get("titles"):
            print(f"🎁 到账明细: {' + '.join(data['titles'])}")
    else:
        result["error"] = result["message"]
        print(f"⚠️ {result['message']}")

    # 余额与用量是辅助信息，读不到不影响签到结论
    balance = data.get("balance")
    if balance:
        result["balance"] = balance
        print(f"💰 可用魔粒: {balance.get('available')}"
              f"（总计 {balance.get('total')}，冻结 {balance.get('frozen')}）")
    elif data.get("error_balance"):
        print(f"⚠️ 余额查询失败: {data['error_balance']}")
        result["error_balance"] = data["error_balance"]

    if data.get("used") is not None:
        print(f"📊 今日已用: {data['used']} 次")

    if dry_run:
        print("ℹ️ 只读预览，未触发签到")

    return _report(result)


def _report(result):
    print("=" * 50)
    return result


def send_notification_summary(all_results, start_time, end_time, dry_run=False):
    """汇总推送。run_all.py 统一调度时由 CHECKIN_NOTIFY_SILENT 抑制。"""
    try:
        duration = (end_time - start_time).total_seconds()
        total = len(all_results)
        ok_count = sum(1 for r in all_results if r.get("success"))
        failed = total - ok_count
        token_issues = [r for r in all_results if r.get("needs_token_fix")]

        if failed == 0:
            title = "魔搭签到成功 ✅"
            sound = NotificationSound.BIRDSONG
        elif ok_count == 0:
            title = "魔搭签到失败 ❌"
            sound = NotificationSound.ALARM
        else:
            title = "魔搭签到部分成功 ⚠️"
            sound = NotificationSound.BELL
        if dry_run:
            title = "魔搭只读预览 " + title.split(" ", 1)[-1]

        parts = [
            "📊 执行统计:",
            f"✅ 成功: {ok_count} 个账号",
            f"❌ 失败: {failed} 个账号",
            f"📈 总计: {total} 个账号",
            "",
            "📝 详情:",
        ]
        for item in all_results:
            name = item.get("account_name", "未知账号")
            if item.get("success"):
                detail = item.get("message") or "签到成功"
                balance = item.get("balance")
                if balance:
                    detail += f"（可用 {balance.get('available')}）"
                parts.append(f"  ✅ [{name}] {detail}")
            else:
                error = (item.get("error") or "未知错误")
                if len(error) > 60:
                    error = error[:60] + "..."
                parts.append(f"  ❌ [{name}] {error}")

        if token_issues:
            parts.append("")
            parts.append("🔑 需处理凭证:")
            for item in token_issues:
                parts.append(f"  - {item.get('account_name')}: 令牌无效或权限档位不足")

        parts.append("")
        parts.append(f"⏱️ 执行耗时: {int(duration)}秒")
        parts.append(f"🕐 完成时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        sent = send_notification(
            title=title, content="\n".join(parts), sound=sound)
        if sent:
            print("✅ 推送通知发送成功")
    except Exception as exc:  # noqa: BLE001 推送失败不影响退出码
        print(f"❌ 推送通知失败: {exc}")


def main():
    """返回 0 = 全部成功或今日已签；1 = 存在失败账号或配置缺失。"""
    dry_run = "--dry-run" in sys.argv[1:]
    start_time = datetime.now()

    print(f"\n{'=' * 60}")
    print("## 魔搭 ModelScope 魔粒签到任务开始")
    print(f"## 开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    if dry_run:
        print("## 模式: 只读预览（不触发签到）")
    print(f"{'=' * 60}\n")

    if JITTER_MAX_SECONDS > 0:
        delay = random.randint(0, JITTER_MAX_SECONDS)
        print(f"⏳ 随机启动延迟 {delay} 秒（MODELSCOPE_JITTER_MAX={JITTER_MAX_SECONDS}）")
        time.sleep(delay)

    try:
        config = load_config()
    except Exception as exc:
        print(f"❌ 读取配置失败: {exc}")
        return 1

    accounts = pick_accounts(config)
    if not accounts:
        print("❌ 配置文件中没有找到魔搭账号（config/token.json → modelscope.accounts）")
        print("   每项需包含 token（ms- 开头），可选 account_name / site / proxy")
        return 1

    print(f"共 {len(accounts)} 个账号待处理\n")

    results = []
    for index, account in enumerate(accounts):
        results.append(process_account(account, dry_run=dry_run))
        if index < len(accounts) - 1:
            delay = random.uniform(*ACCOUNT_DELAY_RANGE)
            print(f"\n⏳ 账号间等待 {delay:.1f} 秒…\n")
            time.sleep(delay)

    end_time = datetime.now()
    ok_count = sum(1 for r in results if r.get("success"))

    print(f"\n{'=' * 60}")
    print("📊 执行结果统计")
    print(f"{'=' * 60}")
    print(f"总计: {len(results)} | 成功: {ok_count} | 失败: {len(results) - ok_count}")
    for item in results:
        icon = "✅" if item.get("success") else "❌"
        detail = item.get("message") if item.get("success") else item.get("error")
        print(f"{icon} {item.get('account_name')}: {detail}")
    print(f"\n⏱️ 耗时: {(end_time - start_time).total_seconds():.1f} 秒")

    send_notification_summary(results, start_time, end_time, dry_run=dry_run)

    return 0 if ok_count == len(results) else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
