"""
new Env('什么值得买');
cron: 1 1 1 1 1
"""

"""
什么值得买任务自动化脚本
功能：支持多账号管理和任务自动执行
模块：众测任务模块 + 互动任务模块
版本：2.0
"""

import json
import logging
import time
import random
import sys
import os
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

# 获取当前文件的绝对路径
current_file = os.path.abspath(__file__)
# sign_daily_task/main.py -> sign_daily_task -> smzdm -> script -> ZaiZaiCat-Checkin
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(current_file))))
# smzdm目录 (用于导入api)
smzdm_dir = os.path.dirname(os.path.dirname(current_file))

# 添加必要的路径到sys.path
if project_root not in sys.path:
    sys.path.insert(0, project_root)
if smzdm_dir not in sys.path:
    sys.path.insert(0, smzdm_dir)

# Pillow 等可选依赖装在项目内 vendor/（系统 site-packages 只读，pip 装不进去）
_vendor = os.path.join(project_root, "vendor")
if os.path.isdir(_vendor) and _vendor not in sys.path:
    sys.path.insert(0, _vendor)

from api.api import SmzdmAPI
from service import SmzdmService

from notification import send_notification, NotificationSound

# ==================== 日志配置 ====================
def setup_logger():
    """
    配置统一的日志系统
    同时输出到控制台和文件，使用统一的格式
    """
    # 创建logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # 清除已存在的处理器
    logger.handlers.clear()

    # 统一的日志格式
    log_formatter = logging.Formatter(
        '%(asctime)s | %(levelname)-7s | %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 文件处理器 - 详细日志
    # 注意：原为 '../smzdm_task.log'（相对 CWD）。run_all.py 以项目根为 CWD 调用时
    # 会解析成 /root/smzdm_task.log，在只读根盘上直接抛 Errno 30。改用绝对路径。
    log_dir = Path(__file__).resolve().parent
    log_path = log_dir / "smzdm_task.log"
    file_handler = logging.FileHandler(str(log_path), encoding='utf-8', mode='a')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(log_formatter)

    # 控制台处理器 - 简洁日志
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(log_formatter)

    # 添加处理器
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logging.getLogger(__name__)

# 初始化日志
logger = setup_logger()


class SmzdmTaskManager:
    """什么值得买任务管理器"""

    def __init__(self):
        """

        初始化任务管理器

        Args:
            config_path: 配置文件路径，默认为项目根目录下的config/token.json
        """
        # 使用全局变量并转换为Path对象
        root_path = Path(project_root)
        config_path = root_path / "config" / "token.json"

        self.config_path = config_path
        self.site_name = "什么值得买"
        self.accounts = []
        self.account_results = []  # 收集每个账号的执行结果
        self.load_config()

    def load_config(self):
        """加载配置文件"""
        try:
            logger.info(f"正在读取配置文件: {self.config_path}")

            if not self.config_path.exists():
                logger.error(f"❌ 配置文件不存在: {self.config_path}")
                raise FileNotFoundError(f"配置文件不存在: {self.config_path}")

            with open(self.config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)

                # 获取什么值得买的配置
                smzdm_config = config.get('smzdm', {})
                self.accounts = smzdm_config.get('accounts', [])

                if not self.accounts:
                    logger.warning("配置文件中没有找到什么值得买账号信息")
                else:
                    logger.info(f"✅ 成功加载配置文件，共 {len(self.accounts)} 个账号\n")
        except json.JSONDecodeError as e:
            logger.error(f"❌ 配置文件JSON格式错误: {str(e)}")
            raise
        except Exception as e:
            logger.error(f"❌ 加载配置文件失败: {str(e)}")
            raise

    def print_task_info(self, task: Dict[str, Any]):
        """
        打印任务信息

        Args:
            task: 任务字典
        """
        task_name = task.get('task_name', '未知任务')
        task_status = task.get('task_status', 0)
        task_finished = task.get('task_finished_num', 0)
        task_total = task.get('task_even_num', 0)
        task_desc = task.get('task_description', '')

        # 任务状态映射
        status_map = {
            0: "⚪ 未开始",
            1: "🔵 进行中",
            2: "🟡 未完成",
            3: "🟢 已完成",
            4: "✅ 已领取"
        }
        status_text = status_map.get(task_status, "❓ 未知")

        # 打印奖励信息
        reward_text = ""
        if task.get('task_reward_data'):
            rewards = task['task_reward_data']
            reward_text = ', '.join([f"{r.get('name', '')}{r.get('num', '')}" for r in rewards])
            logger.info(f"       🎁 {reward_text}")

        logger.info(f"    📋 {task_name}: {status_text} ({task_finished}/{task_total}) 🎁 {reward_text}")

    def process_zhongce_tasks(self, api: SmzdmAPI, account_name: str) -> Dict[str, int]:
        """
        处理众测任务模块

        Args:
            api: SmzdmAPI实例
            account_name: 账号名称

        Returns:
            执行统计字典 {success: 成功数, fail: 失败数, skip: 跳过数}
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"🎯 开始处理众测任务模块")
        logger.info(f"{'='*60}")

        try:
            # 创建服务实例
            service = SmzdmService(api)

            # 获取用户能量值信息
            energy_info = {}
            user_data = api.get_user_energy_info()
            if user_data:
                service.print_energy_info(user_data)
                me = (user_data.get('my_energy') or {})
                energy_info = {
                    'total': me.get('my_energy_total', 0),
                    'coupons': me.get('win_conpou_total', 0),
                }
                # 顺带算出「最近一张必中券还差多少能量」，供推送摘要使用
                coupons = ((user_data.get('exchange_win_coupon') or {})
                           .get('win_coupon_list') or [])
                if coupons:
                    cheapest = min(coupons, key=lambda c: c.get('article_energy_total', 0))
                    energy_info['cheapest_need'] = cheapest.get('article_energy_total', 0)

            # 获取活动ID
            activity_id = api.get_activity_id()
            if not activity_id:
                logger.error(f"❌ 获取众测活动ID失败")
                return {'success': 0, 'fail': 0, 'skip': 0, 'energy': energy_info}

            # 获取任务列表
            tasks = api.get_task_list(activity_id)
            if not tasks:
                logger.error(f"❌ 获取众测任务列表失败")
                return {'success': 0, 'fail': 0, 'skip': 0, 'energy': energy_info}

            # 打印任务信息
            logger.info(f"📊 众测任务列表 (共 {len(tasks)} 个):")
            for task in tasks:
                self.print_task_info(task)

            # 执行任务
            logger.info(f"🔧 开始执行众测任务...")
            success_count = 0
            fail_count = 0
            skip_count = 0

            for task in tasks:
                task_name = task.get('task_name', '未知任务')
                task_status = task.get('task_status', 0)

                # 跳过已领取的任务
                if task_status == 4:
                    skip_count += 1
                    continue

                # 执行任务 - 使用service层
                try:
                    result = service.execute_task(task)
                    if result:
                        success_count += 1
                    else:
                        fail_count += 1

                    # 任务之间等待
                    time.sleep(2)
                    delay_time = random.uniform(10, 15)
                    logger.info(f"[{account_name}] 执行任务 {task_name}结束，等待 {delay_time:.2f} 秒...")
                except Exception as e:
                    logger.error(f"    ❌ 执行任务 [{task_name}] 时发生异常: {str(e)}")
                    fail_count += 1

            # 输出执行结果统计
            logger.info(f"📊 众测任务执行统计:")
            logger.info(f"    ✅ 成功: {success_count} 个")
            logger.info(f"    ⚠️  失败: {fail_count} 个")
            logger.info(f"    ⏭️  跳过: {skip_count} 个")

            # 领取任务奖励
            logger.info(f"💰 检查并领取众测任务奖励...")
            self.claim_task_rewards(api, activity_id)

            return {'success': success_count, 'fail': fail_count, 'skip': skip_count,
                    'energy': energy_info}

        except Exception as e:
            logger.error(f"❌ 处理众测任务时发生错误: {str(e)}", exc_info=True)
            return {'success': 0, 'fail': 0, 'skip': 0, 'energy': {}}

    def process_interactive_tasks(self, api: SmzdmAPI, account_name: str) -> Dict[str, int]:
        """
        处理互动任务模块

        Args:
            api: SmzdmAPI实例
            account_name: 账号名称

        Returns:
            执行统计字典 {success: 成功数, fail: 失败数, skip: 跳过数}
        """
        logger.info(f"\n{'='*60}")
        logger.info(f"🎯 开始处理互动任务模块")
        logger.info(f"{'='*60}")

        try:
            # 创建服务实例
            service = SmzdmService(api)

            # 获取互动任务列表
            task_data = api.get_interactive_task_list()
            if not task_data:
                logger.error("❌ 获取互动任务列表失败")
                return {'success': 0, 'fail': 0, 'skip': 0}

            # 检查并领取活动阶段性奖励
            rows = task_data.get('rows', [])
            if rows:
                first_row = rows[0]
                cell_data = first_row.get('cell_data', {})
                activity_reward_status = cell_data.get('activity_reward_status', 0)
                activity_id = cell_data.get('activity_id', '')

                # activity_reward_status为'1'或1表示有阶段性奖励可领取
                if (activity_reward_status == '1' or activity_reward_status == 1) and activity_id:
                    logger.info("🎁 检测到活动阶段性奖励可领取...")
                    if api.receive_activity_reward(activity_id):
                        logger.info(f"    ✅ 活动阶段性奖励领取成功")
                    else:
                        logger.info(f"    ❌ 活动阶段性奖励领取失败")

            # 解析任务列表 - 使用service层
            tasks = service.parse_interactive_tasks(task_data)
            if not tasks:
                logger.warning("⚠️  没有找到可执行的互动任务")
                return {'success': 0, 'fail': 0, 'skip': 0}

            # 组打印任务信息
            logger.info(f"📊 互动任务列表 (共 {len(tasks)} 个):")

            # 执行任务
            logger.info(f"🔧 开始执行互动任务...")
            success_count = 0
            fail_count = 0
            skip_count = 0

            for task in tasks:
                task_id = task.get('task_id', '')
                task_name = task.get('task_name', '未知任务')
                task_status = int(task.get('task_status', '0'))
                task_event_type = task.get('task_event_type', '')

                if task_status == 4:
                    logger.info(f"  ⏭️  任务 [{task_name}] 已领取奖励,跳过")
                    skip_count += 1
                    continue
                elif task_status == 3:
                    # 已完成未领取,尝试领取奖励
                    logger.info(f"  💰 任务 [{task_name}] 已完成,尝试领取奖励...")
                    try:
                        if api.receive_reward(task_id):
                            logger.info(f"    ✅ 任务 [{task_name}] 奖励领取成功")
                            success_count += 1
                        else:
                            logger.info(f"    ❌ 任务 [{task_name}] 奖励领取失败")
                            fail_count += 1
                    except Exception as e:
                        logger.error(f"    ❌ 领取任务 [{task_name}] 奖励时发生异常: {str(e)}")
                        fail_count += 1

                    # 领取奖励后等待
                    time.sleep(1)
                    continue

                # 执行任务 - 使用service层
                try:
                    logger.info(f"  🔨 开始执行任务: [{task_name}] (类型: {task_event_type})")

                    # 根据任务类型执行不同的操作
                    if task_event_type == "interactive.view.article":
                        # 浏览文章任务
                        result = service.execute_interactive_task(task)
                        if result:
                            success_count += 1
                            logger.info(f"    ✅ 任务 [{task_name}] 执行成功")
                        else:
                            fail_count += 1
                            logger.info(f"    ❌ 任务 [{task_name}] 执行失败")

                    elif task_event_type == "interactive.follow.user":
                        # 关注用户任务 - 现在支持自动执行
                        logger.info(f"    📌 任务 [{task_name}] 类型为关注用户，开始执行关注任务")

                        # 根据任务要求的数量执行关注任务
                        task_finished_num = int(task.get('task_finished_num', 0))
                        task_even_num = int(task.get('task_even_num', 0))
                        remaining_count = task_even_num - task_finished_num

                        if remaining_count <= 0:
                            logger.info(f"    ✅ 任务 [{task_name}] 已完成所有关注 ({task_finished_num}/{task_even_num})")
                            success_count += 1
                            continue

                        # 执行关注任务，数量不超过剩余需要的数量 - 使用service层
                        follow_count = min(remaining_count, 5)  # 每次最多关注5个用户
                        result = service.execute_follow_task(follow_count)
                        if result['success'] > 0:
                            success_count += 1
                            logger.info(f"    ✅ 任务 [{task_name}] 执行成功")
                        else:
                            fail_count += 1
                            logger.info(f"    ❌ 任务 [{task_name}] 执行失败")

                    elif task_event_type == "interactive.comment":
                        # 评论任务
                        logger.warning(f"    ⚠️  任务 [{task_name}] 类型为评论，暂不支持自动执行")
                        fail_count += 1

                    elif task_event_type in ["publish.baoliao_new", "publish.biji_new", "publish.yuanchuang_new", "publish.zhuanzai"]:
                        # 发布类任务（爆料、笔记、原创、推荐）
                        logger.warning(f"    ⚠️  任务 [{task_name}] 类型为发布内容，暂不支持自动执行")
                        fail_count += 1

                    else:
                        logger.warning(f"    ⚠️  未知任务类型: {task_event_type}")
                        fail_count += 1

                    # 任务之间等待
                    time.sleep(2)
                except Exception as e:
                    logger.error(f"    ❌ 执行任务 [{task_name}] 时发生异常: {str(e)}")
                    fail_count += 1

            # 输出执行结果统计
            logger.info(f"📊 互动任务执行统计:")
            logger.info(f"    ✅ 成功: {success_count} 个")
            logger.info(f"    ⚠️  失败: {fail_count} 个")
            logger.info(f"    ⏭️  跳过: {skip_count} 个")

            # 重新获取任务列表并领取奖励
            logger.info(f"💰 重新获取互动任务状态并领取奖励...")
            self.claim_interactive_task_rewards(api, service)

            return {'success': success_count, 'fail': fail_count, 'skip': skip_count}

        except Exception as e:
            logger.error(f"❌ 处理互动任务时发生错误: {str(e)}", exc_info=True)
            return {'success': 0, 'fail': 0, 'skip': 0}

    def claim_task_rewards(self, api: SmzdmAPI, activity_id: str):
        """
        查询任务列表并领取所有可领取的奖励

        Args:
            api: SmzdmAPI实例
            activity_id: 活动ID
        """
        try:
            # 重新获取任务列表，查看最新状态
            tasks = api.get_task_list(activity_id)

            if not tasks:
                logger.warning("    ⚠️  重新获取任务列表失败，无法领取奖励")
                return

            # 筛选出已完成但未领取的任务（状态为3）
            claimable_tasks = [task for task in tasks if task.get('task_status') == 3]

            if not claimable_tasks:
                logger.info("    ℹ️  没有可领取的任务奖励")
                return

            logger.info(f"    🎁 发现 {len(claimable_tasks)} 个可领取奖励的任务")

            # 逐个领取奖励
            claimed_count = 0
            failed_count = 0

            for task in claimable_tasks:
                task_id = task.get('task_id', '')
                task_name = task.get('task_name', '未知任务')

                # 显示奖励信息
                reward_text = ""
                if task.get('task_reward_data'):
                    rewards = task['task_reward_data']
                    reward_text = ', '.join([f"{r.get('name', '')}{r.get('num', '')}" for r in rewards])

                # 调用领取奖励接口
                if api.receive_reward(task_id):
                    claimed_count += 1
                    logger.info(f"      ✅ [{task_name}] 奖励领取成功: {reward_text}")
                else:
                    failed_count += 1

                # 领取间隔
                time.sleep(1)

            # 统计信息
            if claimed_count > 0 or failed_count > 0:
                logger.info(f"  📊 奖励领取结果: 成功 {claimed_count} 个, 失败 {failed_count} 个")

        except Exception as e:
            logger.error(f"    ❌ 领取奖励过程中发生错误: {str(e)}")

    def claim_interactive_task_rewards(self, api: SmzdmAPI, service: SmzdmService):
        """
        重新获取互动任务列表并领取所有可领取的奖励

        Args:
            api: SmzdmAPI实例
            service: SmzdmService实例
        """
        try:
            # 重新获取互动任务列表
            task_data = api.get_interactive_task_list()
            if not task_data:
                logger.warning("    ⚠️  重新获取互动任务列表失败，无法领取奖励")
                return

            # 解析任务列表 - 使用service层
            tasks = service.parse_interactive_tasks(task_data)
            if not tasks:
                logger.warning("    ⚠️  没有找到互动任务")
                return

            # 筛选出已完成但未领取的任务（状态为'3'）
            claimable_tasks = [task for task in tasks if task.get('task_status') == '3']

            if not claimable_tasks:
                logger.info("    ℹ️  没有可领取的互动任务奖励")
                return

            logger.info(f"    🎁 发现 {len(claimable_tasks)} 个可领取奖励的互动任务")

            # 逐个领取奖励
            claimed_count = 0
            failed_count = 0

            for task in claimable_tasks:
                task_id = task.get('task_id', '')
                task_name = task.get('task_name', '未知任务')

                # 调用领取奖励接口
                if api.receive_reward(task_id):
                    claimed_count += 1
                    logger.info(f"      ✅ [{task_name}] 奖励领取成功")
                else:
                    failed_count += 1
                    logger.info(f"      ❌ [{task_name}] 奖励领取失败")

                # 领取间隔
                time.sleep(1)

            # 统计信息
            if claimed_count > 0 or failed_count > 0:
                logger.info(f"  📊 互动任务奖励领取结果: 成功 {claimed_count} 个, 失败 {failed_count} 个")

        except Exception as e:
            logger.error(f"    ❌ 领取互动任务奖励过程中发生错误: {str(e)}")

    def send_task_notification(self, start_time: datetime, end_time: datetime) -> None:
        """
        发送任务执行汇总推送通知

        Args:
            start_time: 任务开始时间
            end_time: 任务结束时间
        """
        try:
            duration = (end_time - start_time).total_seconds()

            # 计算成功和失败数量
            success_count = sum(1 for r in self.account_results if r.get('success'))
            fail_count = len(self.account_results) - success_count

            # 构建推送标题
            if fail_count == 0:
                title = f"{self.site_name}任务完成 ✅"
            else:
                title = f"{self.site_name}任务完成 ⚠️"

            # 构建推送内容
            content_parts = [
                "📊 执行摘要",
                "━━━━━━━━━━━━━━━━",
                f"👥 账号总数: {len(self.account_results)}个",
                f"✅ 成功: {success_count}个",
            ]

            if fail_count > 0:
                content_parts.append(f"❌ 失败: {fail_count}个")

            content_parts.extend([
                f"⏱️ 总耗时: {int(duration)}秒",
                "",
                "📋 账号详情",
                "━━━━━━━━━━━━━━━━"
            ])

            # 添加每个账号的详细信息
            for i, result in enumerate(self.account_results, 1):
                account_name = result.get('account_name', f'账号{i}')

                if not result.get('success'):
                    # 失败账号
                    error = result.get('error', '未知错误')
                    content_parts.append(f"❌ [{account_name}]")
                    content_parts.append(f"   错误: {error}")
                else:
                    # 成功账号
                    checkin = result.get('checkin', {})
                    zhongce = result.get('zhongce', {})
                    interactive = result.get('interactive', {})

                    content_parts.append(f"✅ [{account_name}]")

                    # 签到信息
                    if checkin.get('success'):
                        days = checkin.get('continuous_days', 0)
                        if days > 0:
                            content_parts.append(f"   📅 签到: 连续{days}天")
                        else:
                            content_parts.append(f"   📅 签到: 成功")
                        # 账户余额：只展示新版体系真实存在的三项。
                        # 旧的「积分(cpoints)」已废弃恒为 0，展示出来纯属误导，故不显示。
                        gold = checkin.get('gold', '0')
                        silver = checkin.get('silver', '0')
                        exp = checkin.get('experience', '0')
                        content_parts.append(f"   💰 金币{gold} · 碎银子{silver} · 经验{exp}")

                    # 能量值与必中券：这是攒实物奖品的唯一路径，必须出现在摘要里
                    energy = zhongce.get('energy') or {}
                    if energy.get('total') is not None:
                        line = f"   💎 能量值 {energy.get('total', 0)} · 必中券 {energy.get('coupons', 0)} 张"
                        need = energy.get('cheapest_need')
                        if need and not energy.get('coupons'):
                            gap = max(int(need) - int(energy.get('total') or 0), 0)
                            line += f"（最近门槛 {need}，还差 {gap}）"
                        content_parts.append(line)

                    # 众测任务统计。失败项多为活动已下架，不必逐条推送
                    z_success = zhongce.get('success', 0)
                    z_fail = zhongce.get('fail', 0)
                    z_skip = zhongce.get('skip', 0)
                    line = f"   🎯 众测: ✅{z_success} ⏭️{z_skip}"
                    if z_fail:
                        line += f" ⚠️{z_fail}(活动已下架)"
                    content_parts.append(line)

                    # 互动任务统计。失败项多为接口风控(403)，同样只给数字
                    i_success = interactive.get('success', 0)
                    i_fail = interactive.get('fail', 0)
                    i_skip = interactive.get('skip', 0)
                    line = f"   🎯 互动: ✅{i_success} ⏭️{i_skip}"
                    if i_fail:
                        line += f" ⚠️{i_fail}(接口风控)"
                    content_parts.append(line)

                # 账号之间添加空行
                if i < len(self.account_results):
                    content_parts.append("")

            # 添加完成时间
            content_parts.append("━━━━━━━━━━━━━━━━")
            content_parts.append(f"🕐 {end_time.strftime('%Y-%m-%d %H:%M:%S')}")

            content = "\n".join(content_parts)

            # 发送推送
            send_notification(
                title=title,
                content=content,
                sound=NotificationSound.BIRDSONG,
                group=self.site_name
            )
            logger.info(f"✅ {self.site_name}任务汇总推送发送成功")

        except Exception as e:
            logger.error(f"❌ 发送任务汇总推送失败: {str(e)}", exc_info=True)

    def process_account(self, account: Dict[str, str]) -> Dict[str, Any]:
        """
        处理单个账号的任务

        Args:
            account: 账号信息字典

        Returns:
            账号执行结果统计
        """
        account_name = account.get('name', '未命名账号')
        cookie = account.get('cookie', '')
        user_agent = account.get('user_agent', '')
        setting = account.get('setting', '')

        # 初始化结果
        result = {
            'account_name': account_name,
            'success': False,
            'error': None,
            'checkin': {'success': False, 'continuous_days': 0},
            'zhongce': {'success': 0, 'fail': 0, 'skip': 0},
            'interactive': {'success': 0, 'fail': 0, 'skip': 0}
        }

        if not cookie or not user_agent:
            logger.error(f"❌ 账号 [{account_name}] 配置不完整，跳过\n")
            result['error'] = '配置不完整'
            return result

        logger.info(f"{'='*60}")
        logger.info(f"👤 账号: {account_name}")
        logger.info(f"{'='*60}")

        # 创建API客户端
        api = SmzdmAPI(cookie, user_agent, setting)

        try:
            # 创建服务实例
            service = SmzdmService(api)

            # 0. 每日签到
            logger.info(f"\n{'='*60}")
            logger.info(f"📅 开始执行每日签到")
            logger.info(f"{'='*60}")

            checkin_data = api.daily_checkin()
            if checkin_data:
                service.print_checkin_info(checkin_data)
                result['checkin']['success'] = True
                # 连续签到天数：真实字段是 data.daily_num（2026-10-26 实测），
                # 上游读的 continue_checkin_days 在响应里不存在，永远是 0。
                data = checkin_data.get('data') or {}
                days = data.get('daily_num') or data.get('continue_checkin_days') or 0
                result['checkin']['continuous_days'] = int(days or 0)
                # 新版账户体系以「金币 / 碎银子 / 经验」计量，旧「积分(cpoints)」已废弃
                # （个人中心只显示这三项，积分日志页为「暂无积分记录」）。
                # 保存下来供推送摘要使用，避免再展示恒为 0 的「积分」。
                result['checkin']['gold'] = data.get('cgold', '0')
                result['checkin']['silver'] = data.get('silver', data.get('csilver', '0'))
                result['checkin']['experience'] = data.get('cexperience', '0')
                result['checkin']['points_added'] = data.get('cpadd', '0')
            else:
                logger.warning("⚠️  每日签到失败或已签到")

            # 等待一下再处理下一个模块
            time.sleep(2)

            # 1. 处理众测任务
            zhongce_stats = self.process_zhongce_tasks(api, account_name)
            result['zhongce'] = zhongce_stats

            # 等待一下再处理下一个模块
            delay_time = random.uniform(10, 15)
            logger.info(f"[{account_name}] 更换任务模块 {delay_time}，等待 {delay_time:.2f} 秒...")

            # 2. 处理互动任务
            interactive_stats = self.process_interactive_tasks(api, account_name)
            result['interactive'] = interactive_stats

            # 输出总统计
            logger.info(f"\n{'='*60}")
            logger.info(f"📈 账号 [{account_name}] 总体统计")
            logger.info(f"{'='*60}")
            logger.info(f"  📅 每日签到: {'✅成功' if result['checkin']['success'] else '❌失败'}")
            logger.info(f"  🎯 众测任务: ✅{zhongce_stats['success']} ⚠️{zhongce_stats['fail']} ⏭️{zhongce_stats['skip']}")
            logger.info(f"  🎯 互动任务: ✅{interactive_stats['success']} ⚠️{interactive_stats['fail']} ⏭️{interactive_stats['skip']}")

            total_success = zhongce_stats['success'] + interactive_stats['success']
            total_fail = zhongce_stats['fail'] + interactive_stats['fail']
            total_skip = zhongce_stats['skip'] + interactive_stats['skip']

            logger.info(f"  📊 任务总计: ✅成功 {total_success} | ⚠️失败 {total_fail} | ⏭️跳过 {total_skip}")
            logger.info(f"\n✨ 账号 [{account_name}] 处理完成\n")

            result['success'] = True
            return result

        except Exception as e:
            logger.error(f"❌ 处理账号 [{account_name}] 时发生错误: {str(e)}\n", exc_info=True)
            result['error'] = str(e)
            return result
        finally:
            api.close()

    def run(self):
        """运行任务管理器"""
        start_time = datetime.now()

        logger.info("\n" + "="*60)
        logger.info("🎉 什么值得买任务自动化脚本")
        logger.info(f"⏰ {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("="*60 + "\n")

        if not self.accounts:
            logger.error("❌ 没有配置任何账号，请检查配置文件\n")
            return

        logger.info(f"📝 共配置 {len(self.accounts)} 个账号\n")

        # 处理每个账号
        for idx, account in enumerate(self.accounts, 1):
            try:
                logger.info(f"\n{'#'*60}")
                logger.info(f"# 处理第 {idx}/{len(self.accounts)} 个账号")
                logger.info(f"{'#'*60}\n")

                # 处理账号并收集结果
                result = self.process_account(account)
                self.account_results.append(result)

                # 如果不是最后一个账号,等待一段时间
                if idx < len(self.accounts):
                    wait_time = 5
                    logger.info(f"⏳ 等待 {wait_time} 秒后处理下一个账号...\n")
                    time.sleep(wait_time)

            except Exception as e:
                logger.error(f"❌ 处理第 {idx} 个账号时发生错误: {str(e)}\n", exc_info=True)
                # 记录失败的账号
                self.account_results.append({
                    'account_name': account.get('name', f'账号{idx}'),
                    'success': False,
                    'error': str(e)
                })
                continue

        end_time = datetime.now()
        duration = end_time - start_time

        logger.info("\n" + "="*60)
        logger.info("🎊 所有账号处理完成")
        logger.info(f"⏰ {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"⏱️  总耗时: {duration.total_seconds():.2f} 秒")
        logger.info("="*60 + "\n")


def main():
    """主函数"""
    # 记录开始时间
    start_time = datetime.now()
    print(f"\n{'='*60}")
    print(f"## 什么值得买任务开始")
    print(f"## 开始时间: {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    logger.info("="*60)
    logger.info(f"什么值得买任务开始执行 - {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("="*60)

    try:
        # 创建任务管理器（默认读取 config/token.json）
        manager = SmzdmTaskManager()

        # 运行任务
        manager.run()

        # 记录结束时间
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        print(f"\n{'='*60}")
        print(f"## 什么值得买任务完成")
        print(f"## 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"## 执行耗时: {int(duration)} 秒")
        print(f"{'='*60}\n")

        logger.info("="*60)
        logger.info(f"什么值得买任务执行完成 - {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"执行耗时: {int(duration)} 秒")
        logger.info("="*60)

        # 发送任务汇总推送
        if manager.account_results:
            manager.send_task_notification(start_time, end_time)

        return 0

    except Exception as e:
        end_time = datetime.now()
        duration = (end_time - start_time).total_seconds()

        logger.error(f"任务执行异常: {str(e)}", exc_info=True)

        print(f"\n{'='*60}")
        print(f"## ❌ 任务执行异常")
        print(f"## 错误信息: {str(e)}")
        print(f"## 结束时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"## 执行耗时: {int(duration)} 秒")
        print(f"{'='*60}\n")

        # 发送错误通知
        try:
            send_notification(
                title="什么值得买任务异常 ❌",
                content=(
                    f"❌ 任务执行异常\n"
                    f"💬 错误信息: {str(e)}\n"
                    f"⏱️ 执行耗时: {int(duration)}秒\n"
                    f"🕐 完成时间: {end_time.strftime('%Y-%m-%d %H:%M:%S')}"
                ),
                sound=NotificationSound.ALARM,
                group="什么值得买"
            )
        except:
            pass

        return 1


if __name__ == "__main__":
    sys.exit(main())
