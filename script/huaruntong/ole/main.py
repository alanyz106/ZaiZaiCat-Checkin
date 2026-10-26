"""
new Env('华润通Ole签到');
cron: 1 1 1 1 1
"""

"""
Ole 签到主程序
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from api import OleAPI

# 添加项目根目录到Python路径以导入notification模块
current_dir = Path(__file__).parent
project_root = current_dir.parent.parent.parent
sys.path.insert(0, str(project_root))

from notification import send_notification, NotificationSound


def load_config():
    """加载统一配置文件"""
    # 使用统一的 token.json 配置文件
    # 从当前文件位置向上三级到达项目根目录，然后进入 config 目录
    current_dir = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(current_dir, '..', '..', '..', 'config', 'token.json')
    with open(config_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def process_account(account_config):
    """处理单个账号的签到"""
    account_name = account_config.get('account_name', '未知账号')

    # 初始化结果
    result_info = {
        'account_name': account_name,
        'device_name': account_config["device_name"],
        'success': False,
        'error': None,
        'message': None
    }

    # 初始化 API
    api = OleAPI(
        session_id=account_config["session_id"],
        device_name=account_config["device_name"],
        unique=account_config["unique"],
        ole_wx_open_id=account_config["ole_wx_open_id"],
        shop_code=account_config["shop_code"],
        city_id=account_config["city_id"],
        user_agent=account_config.get("user_agent"),
        app_version=account_config.get("appVersion", "1.11.4"),
    )

    print("=" * 50)
    print(f"账号: {account_name} ({account_config.get('device_name', '?')})")
    print("=" * 50)

    # 执行签到
    print("\n开始签到...")
    result = api.sign_in()

    # 处理签到结果
    # 成功口径（抓包实测）：{"state_code":200,"data":{"integral":5,"sign_day":1}}
    # 已签口径（2026-10-26 实测）：{"state_code":3001,"data":{},"message":"当日签到已完成，请勿重复签到！"}
    # ⚠️ 两个坑：
    #   1) 成功/业务失败用 state_code（下划线），鉴权失败用 stateCode（驼峰），别混。
    #   2) 业务失败的原因在 **message**（不是 msg）；但鉴权失败用的是 msg。
    #      2026-10-26 线上就因此把「今日已签」误判成失败，两处都要读。
    if "error" in result:
        error_msg = result['error']
        print(f"❌ 签到失败: {error_msg}")
        result_info['error'] = error_msg
    else:
        state_code = str(result.get("state_code") or "")
        text = str(result.get("message") or result.get("msg") or "")
        data = result.get("data") or {}

        if state_code == "200":
            integral = data.get("integral")
            sign_day = data.get("sign_day")
            result_info['success'] = True
            result_info['message'] = f"签到成功 +{integral} 积分" if integral is not None else "签到成功"
            if sign_day:
                result_info['message'] += f"（第 {sign_day} 天）"
            print(f"✅ {result_info['message']}")
            print(f"响应数据: {json.dumps(result, ensure_ascii=False)}")
        elif state_code == "3001" or "已签" in text or "已完成" in text:
            # 今日已签不是失败
            result_info['success'] = True
            result_info['message'] = "今日已签到"
            print(f"✅ 今日已签到: {text}")
        else:
            error_msg = text or f"未知响应: {json.dumps(result, ensure_ascii=False)[:120]}"
            print(f"❌ 签到失败: {error_msg}")
            result_info['error'] = error_msg

    print("\n" + "=" * 50)
    return result_info


def send_notification_summary(all_results, start_time, end_time):
    """
    发送任务执行结果的推送通知

    Args:
        all_results: 所有账号的执行结果列表
        start_time: 任务开始时间
        end_time: 任务结束时间
    """
    try:
        duration = (end_time - start_time).total_seconds()

        # 统计结果
        total_count = len(all_results)
        success_count = sum(1 for r in all_results if r.get('success'))
        failed_count = total_count - success_count

        # 构建通知标题
        if failed_count == 0:
            title = "Ole签到成功 ✅"
            sound = NotificationSound.BIRDSONG
        elif success_count == 0:
            title = "Ole签到失败 ❌"
            sound = NotificationSound.ALARM
        else:
            title = "Ole签到部分成功 ⚠️"
            sound = NotificationSound.BELL

        # 构建通知内容
        content_parts = [
            "📊 执行统计:",
            f"✅ 成功: {success_count} 个账号",
            f"❌ 失败: {failed_count} 个账号",
            f"📈 总计: {total_count} 个账号",
            "",
            "📝 详情:"
        ]

        for result in all_results:
            account_name = result.get('account_name', '未知账号')
            if result.get('success'):
                content_parts.append(f"  ✅ [{account_name}] 签到成功")
            else:
                error = result.get('error', '未知错误')
                if len(error) > 30:
                    error = error[:30] + "..."
                content_parts.append(f"  ❌ [{account_name}] {error}")

        content_parts.append("")
        content_parts.append(f"⏱️ 执行耗时: {int(duration)}秒")
        content_parts.append(f"🕐 完成时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

        content = "\n".join(content_parts)

        # 发送通知（静默模式下返回 False，由 run_all.py 统一汇总）
        sent = send_notification(
            title=title,
            content=content,
            sound=sound,
            group="Ole精品超市"
        )
        if sent:
            print("✅ 推送通知发送成功")

    except Exception as e:
        print(f"❌ 推送通知失败: {str(e)}")


def main():
    """主函数"""
    # 记录开始时间
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"## Ole签到任务开始")
    print(f"## 开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # 加载配置
    config = load_config()
    accounts = config.get('huaruntong', {}).get('ole', {}).get('accounts', [])

    if not accounts:
        print("❌ 配置文件中没有找到账号信息 (huaruntong.ole.accounts)")
        return 1

    # 收集所有账号的结果
    all_results = []

    # 遍历所有账号
    for account in accounts:
        if not account.get('session_id'):
            print(f"⚠️  跳过账号 {account.get('account_name', '未知')}: session_id 为空")
            print("=" * 50)
            all_results.append({
                'account_name': account.get('account_name', '未知'),
                'success': False,
                'error': 'session_id为空'
            })
            continue

        result = process_account(account)
        all_results.append(result)
        print("\n")

    # 记录结束时间
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()

    success_count = sum(1 for r in all_results if r.get('success'))
    print(f"\n{'='*60}")
    print(f"## Ole签到任务完成")
    print(f"## 成功: {success_count}/{len(all_results)}  失败: {len(all_results) - success_count}")
    print(f"## 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"## 执行耗时: {int(duration)} 秒")
    print(f"{'='*60}\n")

    # 发送推送通知（run_all.py 调度时会因 CHECKIN_NOTIFY_SILENT=1 自动跳过）
    send_notification_summary(all_results, start_time, end_time)

    failed = [r for r in all_results if not r.get('success')]
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main() or 0)
