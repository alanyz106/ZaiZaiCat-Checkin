"""
new Env('华润通微信版签到');
cron: 1 1 1 1 1
"""

"""
华润通签到主程序
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from api import HuaRunTongAPI

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
        'success': False,
        'error': None,
        'message': None,
        'response': None
    }

    print("=" * 50)
    print(f"账号: {account_name}")
    print("=" * 50)

    # 初始化API
    api = HuaRunTongAPI(
        token=account_config.get("token"),
        answer_result=account_config.get("answerResult", 1),
        channel_id=account_config.get("channelId", "APP"),
        merchant_code=account_config.get("merchantCode", "1641000001532"),
        store_code=account_config.get("storeCode", "qiandaosonjifen"),
        sys_id=account_config.get("sysId", "T0000001"),
        transaction_uuid=account_config.get("transactionUuid", ""),
        invite_code=account_config.get("inviteCode", ""),
        user_agent=account_config.get("user_agent")
    )

    # 发送请求
    print("\n发送签到请求...")
    result = api.sign_in()

    # 解析结果
    # 成功口径（2026-10-26 实测）：
    #   S0A00000            → 本次签到成功，data.integral 为本次获得的积分
    #   E1B00101 "不能重复签到" → 今日已签，属于「正常」而非失败，退出码按成功算
    code = result.get('code')
    msg = result.get('msg') or result.get('message') or ''
    if code == "S0A00000":
        result_info['success'] = True
        result_info['message'] = msg or '签到成功'
        result_info['response'] = result
        print(f"✅ 签到成功: {result_info['message']}")
    elif code == "E1B00101" or "不能重复签到" in msg or "已签到" in msg:
        result_info['success'] = True
        result_info['message'] = "今日已签到"
        result_info['response'] = result
        print(f"✅ 今日已签到: {msg}")
    elif result.get('error'):
        # 网络层错误（超时、DNS、连接重置等）
        result_info['error'] = result['error']
        result_info['response'] = result['error']
        print(f"❌ 请求失败: {result_info['error']}")
    else:
        result_info['error'] = msg or f"签到失败(未知响应码 {code})"
        result_info['response'] = msg
        print(f"❌ 签到失败: {result_info['error']}")

    print("响应:")
    print(json.dumps(result, indent=2, ensure_ascii=False))
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
            title = "华润通签到成功 ✅"
            sound = NotificationSound.BIRDSONG
        elif success_count == 0:
            title = "华润通签到失败 ❌"
            sound = NotificationSound.ALARM
        else:
            title = "华润通签到部分成功 ⚠️"
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
                message = result.get('message', '签到成功')
                content_parts.append(f"  ✅ [{account_name}] {message}")
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
            sound=sound
        )
        if sent:
            print("✅ 推送通知发送成功")

    except Exception as e:
        print(f"❌ 推送通知失败: {str(e)}")


def main():
    """主函数，返回退出码：0 = 全部成功/今日已签，1 = 存在失败账号或配置缺失"""
    # 记录开始时间
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"## 华润通签到任务开始")
    print(f"## 开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    # 加载配置
    config = load_config()
    accounts = config.get('huaruntong', {}).get('huaruntong_wx', {}).get('accounts', [])

    if not accounts:
        print("❌ 配置文件中没有找到账号信息 (huaruntong.huaruntong_wx.accounts)")
        return 1

    # 收集所有账号的结果
    all_results = []

    # 遍历所有账号
    for account in accounts:
        if not account.get('token'):
            print(f"⚠️  跳过账号 {account.get('account_name', '未知')}: token 为空")
            print("=" * 50)
            all_results.append({
                'account_name': account.get('account_name', '未知'),
                'success': False,
                'error': 'token为空'
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
    print(f"## 华润通签到任务完成")
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
