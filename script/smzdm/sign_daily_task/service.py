"""
什么值得买业务逻辑服务模块
功能：处理所有业务逻辑，协调API调用
版本：2.0
"""

import logging
import time
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class SmzdmService:
    """什么值得买业务服务类 - 处理所有业务逻辑"""

    def __init__(self, api):
        """
        初始化业务服务

        Args:
            api: SmzdmAPI实例
        """
        self.api = api

    # ==================== 数据解析相关 ====================

    def parse_interactive_tasks(self, task_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        解析互动任务数据，提取所有任务列表

        Args:
            task_data: 从API获取的任务数据

        Returns:
            任务列表
        """
        all_tasks = []

        row = task_data.get('rows', [])[0]
        if not row:
            logger.warning("互动任务数据中没有找到任务行")
            return all_tasks

        cell_data = row.get('cell_data', {})
        activity_task = cell_data.get('activity_task', {})

        # 获取累计任务列表
        default_list_v2 = activity_task.get('default_list_v2', [])

        # 遍历每个模块的任务列表
        if default_list_v2:
            module = default_list_v2[0]
            task_list = module.get('task_list', [])
            logger.info(f"发现{len(task_list)} 个每日任务")
            return task_list
        else:
            logger.warning("互动任务数据中没有找到任务列表")
            return []

    def print_energy_info(self, user_data: Dict[str, Any]):
        """
        打印用户能量值信息

        Args:
            user_data: 用户数据字典
        """
        my_energy = user_data.get('my_energy', {})
        my_energy_total = my_energy.get('my_energy_total', 0)
        energy_expired_time = my_energy.get('energy_expired_time', '未知')
        win_coupon_total = my_energy.get('win_conpou_total', 0)

        logger.info(f"\n  💎 能量值信息:")
        logger.info(f"    当前能量值: {my_energy_total}")
        logger.info(f"    过期时间: {energy_expired_time}")
        logger.info(f"    已兑换必中券: {win_coupon_total} 张")

        # 显示可兑换的必中券列表
        exchange_info = user_data.get('exchange_win_coupon', {})
        win_coupon_list = exchange_info.get('win_coupon_list', [])

        if win_coupon_list:
            # 只打印「够得着」的券，或最近一张的最低门槛。
            # 全部逐条打印会产生 4 行 ❌（1350/7200/18000/30000 能量值），
            # 而实际签到每天只加 2~10 能量，天天刷这四行纯属噪音。
            affordable = [c for c in win_coupon_list
                          if my_energy_total >= c.get('article_energy_total', 0)]
            if affordable:
                logger.info(f"\n  🎫 可兑换必中券（{len(affordable)}/{len(win_coupon_list)} 张已达标）:")
                for coupon in affordable:
                    logger.info(f"    ✅ {coupon.get('article_title', '未知')} "
                                f"- 需要{coupon.get('article_energy_total', 0)}能量值")
            else:
                cheapest = min(win_coupon_list, key=lambda c: c.get('article_energy_total', 0))
                need = cheapest.get('article_energy_total', 0)
                logger.info(f"\n  🎫 必中券: 暂无达标（最低 {need} 能量值，"
                            f"当前 {my_energy_total}，还差 {max(need - my_energy_total, 0)}）")

    # ==================== 众测任务业务逻辑 ====================

    def execute_task(self, task: Dict[str, Any]) -> bool:
        """
        根据任务类型执行对应的任务（众测任务）

        Args:
            task: 任务信息字典

        Returns:
            是否成功
        """
        task_id = task.get('task_id', '')
        task_name = task.get('task_name', '未知任务')
        task_event_type = task.get('task_event_type', '')
        task_status = task.get('task_status', 0)
        channel_id = task.get('channel_id', 0)
        article_id = task.get('article_id', '')

        # 任务状态: 0-未开始, 1-进行中, 2-未完成, 3-已完成, 4-已领取
        if task_status == 4:
            logger.info(f"任务 [{task_name}] 已领取奖励,跳过")
            return True
        elif task_status == 3:
            # 已完成未领取,尝试领取奖励
            logger.info(f"任务 [{task_name}] 已完成,尝试领取奖励...")
            return self.api.receive_reward(task_id)

        logger.info(f"开始执行任务: {task_name} (类型: {task_event_type})")

        # 根据任务类型执行不同的操作
        if task_event_type == "interactive.view.article":
            # 浏览文章任务

            return self.api.view_article_task(task_id, article_id, channel_id, task_event_type)

        elif task_event_type == "interactive.favorite":
            # 收藏文章任务
            redirect_url = task.get('task_redirect_url', {})
            article_link_val = redirect_url.get('link_val', '')

            if not article_link_val:
                logger.warning(f"任务 [{task_name}] 缺少文章ID,跳过")
                return False

            return self.api.favorite_article_task(task_id, article_link_val)

        elif task_event_type == "interactive.rating":
            # 点赞文章任务
            redirect_url = task.get('task_redirect_url', {})
            article_link_val = redirect_url.get('link_val', '')

            if not article_link_val:
                logger.warning(f"任务 [{task_name}] 缺少文章ID,跳过")
                return False

            return self.api.rating_article_task(task_id, article_link_val)

        elif task_event_type == "guide.apply_zhongce":
            # 申请众测任务
            return self.execute_apply_zhongce_task(task)

        elif task_event_type == "interactive.share":
            # 分享众测招募任务
            return self.execute_share_task(task)

        else:
            logger.warning(f"未知任务类型: {task_event_type}")
            return False

    def execute_share_task(self, task: Dict[str, Any]) -> bool:
        """
        执行分享众测招募任务

        Args:
            task: 任务信息字典

        Returns:
            是否成功
        """
        task_name = task.get('task_name', '未知任务')
        task_finished_num = task.get('task_finished_num', 0)
        task_even_num = task.get('task_even_num', 0)

        # 计算还需要分享的次数
        remaining_count = task_even_num - task_finished_num

        if remaining_count <= 0:
            logger.info(f"任务 [{task_name}] 已完成所有分享 ({task_finished_num}/{task_even_num})")
            return True

        logger.info(f"任务 [{task_name}] 需要分享 {remaining_count} 次 (已完成 {task_finished_num}/{task_even_num})")

        # 获取众测列表
        probation_list = self.api.get_probation_list(status='all')
        if not probation_list:
            logger.error("获取众测列表失败，无法完成分享任务")
            return False

        # 提取可分享的众测商品信息
        available_shares = []
        for item in probation_list:
            article_id = item.get('article_id', '')
            article_channel_id = item.get('article_channel_id', '')
            article_title = item.get('article_title', '未知商品')

            if article_id and article_channel_id:
                available_shares.append({
                    'article_id': article_id,
                    'channel_id': article_channel_id,
                    'title': article_title
                })

        if not available_shares:
            logger.warning("当前没有可分享的众测商品")
            return False

        logger.info(f"找到 {len(available_shares)} 个可分享的众测商品")

        # 开始分享
        success_count = 0
        for i, share_item in enumerate(available_shares):
            if success_count >= remaining_count:
                break

            logger.info(f"  [{i+1}] 分享众测商品: {share_item['title']}")

            # 执行分享
            if self.api.share_probation_task(share_item['article_id'], share_item['channel_id']):
                success_count += 1
                logger.info(f"    ✅ 分享成功 (进度: {success_count}/{remaining_count})")
            else:
                logger.info(f"    ❌ 分享失败")

            # 分享间隔
            if success_count < remaining_count:
                time.sleep(2)

        logger.info(f"分享任务完成，成功分享 {success_count} 次")
        return success_count > 0

    def execute_apply_zhongce_task(self, task: Dict[str, Any]) -> bool:
        """
        执行申请众测任务

        Args:
            task: 任务信息字典

        Returns:
            是否成功
        """
        task_name = task.get('task_name', '未知任务')
        task_finished_num = task.get('task_finished_num', 0)
        task_even_num = task.get('task_even_num', 0)

        # 计算还需要申请的次数
        remaining_count = task_even_num - task_finished_num

        if remaining_count <= 0:
            logger.info(f"任务 [{task_name}] 已完成所有申请 ({task_finished_num}/{task_even_num})")
            return True

        logger.info(f"任务 [{task_name}] 需要申请 {remaining_count} 次 (已完成 {task_finished_num}/{task_even_num})")

        # 获取众测列表
        probation_list = self.api.get_probation_list()
        if not probation_list:
            logger.error("获取众测列表失败，无法完成申请任务")
            return False

        # 过滤出可申请的众测商品
        available_probations = []
        for item in probation_list:
            article_probation = item.get('article_probation', {})
            product_status = article_probation.get('product_status', '')

            # product_status == "1" 表示可申请
            if product_status == '1':
                article_id = item.get('article_id', '')
                article_title = item.get('article_title', '未知商品')
                apply_num = article_probation.get('apply_num', '')
                product_num = article_probation.get('product_num', '')
                product_status_name = article_probation.get('product_status_name', '')

                available_probations.append({
                    'id': article_id,
                    'title': article_title,
                    'apply_num': apply_num,
                    'product_num': product_num,
                    'status_name': product_status_name
                })

        if not available_probations:
            logger.warning("当前没有可申请的众测商品")
            return False

        logger.info(f"找到 {len(available_probations)} 个可申请的众测商品")

        # 开始申请
        success_count = 0
        for i, probation in enumerate(available_probations):
            if success_count >= remaining_count:
                break

            logger.info(f"  [{i+1}] {probation['title']} - {probation['apply_num']} - {probation['status_name']}")

            # 提交申请
            if self.api.submit_probation_apply(probation['id']):
                success_count += 1
                logger.info(f"    ✅ 申请成功 (进度: {success_count}/{remaining_count})")
            else:
                logger.info(f"    ⏭️  跳过该商品")

            # 申请间隔
            if success_count < remaining_count:
                time.sleep(1)

        logger.info(f"众测申请任务完成，成功申请 {success_count} 次")
        return success_count > 0

    # ==================== 互动任务业务逻辑 ====================

    def execute_interactive_task(self, task: Dict[str, Any]) -> bool:
        """
        执行互动任务

        Args:
            task: 任务信息字典

        Returns:
            是否成功
        """
        task_id = task.get('task_id', '')
        task_name = task.get('task_name', '未知任务')
        task_event_type = task.get('task_event_type', '')
        task_status = task.get('task_status', '0')
        task_finished_num = int(task.get('task_finished_num', 0))
        task_even_num = int(task.get('task_even_num', 0))
        module_name = task.get('module_name', '未知模块')

        # 任务状态: "2"-未完成, "3"-已完成, "4"-已领取
        if task_status == '4':
            logger.info(f"[{module_name}] 任务 [{task_name}] 已领取奖励，跳过")
            return True

        # 检查任务是否已完成
        if task_finished_num >= task_even_num:
            logger.info(f"[{module_name}] 任务 [{task_name}] 已完成 ({task_finished_num}/{task_even_num})")
            return True

        logger.info(f"[{module_name}] 开始执行任务: {task_name} (类型: {task_event_type}, 进度: {task_finished_num}/{task_even_num})")

        # 根据任务类型执行不同的操作
        if task_event_type == "interactive.view.article":
            # 浏览文章任务
            article_id = task.get('article_id', '')
            channel_id = task.get('channel_id', '0')

            if not article_id or article_id == '0':
                logger.warning(f"任务 [{task_name}] 缺少文章ID，跳过")
                return False

            # 如果channel_id为0或未提供，尝试通过article_id获取
            if not channel_id or channel_id == '0':
                fetched_channel_id = self.api.get_article_channel_id(article_id)
                if fetched_channel_id is not None:
                    channel_id = str(fetched_channel_id)
                else:
                    logger.warning(f"任务 [{task_name}] 无法获取channel_id，使用默认值")
                    channel_id = '3'  # 默认频道ID

            return self.api.view_article_task(task_id, article_id, channel_id, task_event_type)

        elif task_event_type == "interactive.follow.user":
            # 关注用户任务
            logger.warning(f"任务 [{task_name}] 类型为关注用户，暂不支持自动执行")
            return False

        elif task_event_type == "interactive.comment":
            # 评论任务
            logger.warning(f"任务 [{task_name}] 类型为评论，暂不支持自动执行")
            return False

        elif task_event_type in ["publish.baoliao_new", "publish.biji_new", "publish.yuanchuang_new", "publish.zhuanzai"]:
            # 发布类任务（爆料、笔记、原创、推荐）
            logger.warning(f"任务 [{task_name}] 类型为发布内容，暂不支持自动执行")
            return False

        else:
            logger.warning(f"未知任务类型: {task_event_type}")
            return False

    # ==================== 关注用户业务逻辑 ====================

    def execute_follow_task(self, max_follow_count: int = 5) -> Dict[str, int]:
        """
        执行关注任务（关注用户后立即取消关注）

        Args:
            max_follow_count: 最大关注用户数量，默认为5

        Returns:
            执行统计字典 {success: 成功数, fail: 失败数}
        """
        logger.info(f"开始执行关注任务，最大关注用户数: {max_follow_count}")

        success_count = 0
        fail_count = 0

        try:
            # 获取用户列表
            user_data = self.api.get_follow_user_list()
            if not user_data:
                logger.error("获取用户列表失败")
                return {'success': 0, 'fail': 1}

            # 解析用户列表
            rows = user_data.get('rows', [])
            if not rows:
                logger.warning("用户列表为空")
                return {'success': 0, 'fail': 1}

            logger.info(f"获取到 {len(rows)} 个用户")

            processed_count = 0
            for user_row in rows:
                if processed_count >= max_follow_count:
                    break

                # 提取用户信息
                article_title = user_row.get('article_title', '')
                user_id = user_row.get('keyword_id', '')

                if not article_title or not user_id:
                    logger.warning(f"用户信息不完整，跳过: {user_row}")
                    continue

                logger.info(f"  [{processed_count + 1}] 处理用户: {article_title}")

                # 执行关注
                if self.api.follow_user(article_title, user_id):
                    logger.info(f"    ✅ 关注成功")

                    # 等待一下再取消关注
                    time.sleep(2)

                    # 取消关注
                    if self.api.unfollow_user(article_title, user_id):
                        logger.info(f"    ✅ 取消关注成功")
                        success_count += 1
                    else:
                        logger.info(f"    ❌ 取消关注失败")
                        fail_count += 1
                else:
                    logger.info(f"    ❌ 关注失败")
                    fail_count += 1

                processed_count += 1

                # 处理间隔
                if processed_count < max_follow_count:
                    time.sleep(3)

            logger.info(f"关注任务执行完成: 成功 {success_count} 个, 失败 {fail_count} 个")
            return {'success': success_count, 'fail': fail_count}

        except Exception as e:
            logger.error(f"执行关注任务时发生错误: {str(e)}")
            return {'success': success_count, 'fail': fail_count + 1}

    # ==================== 每日签到业务逻辑 ====================

    def print_checkin_info(self, checkin_data: Dict[str, Any]):
        """
        打印签到信息

        Args:
            checkin_data: 签到返回的数据字典
        """
        # 提取签到信息
        cpadd = checkin_data.get('cpadd', 0)  # 本次新增积分
        daily_num = checkin_data.get('daily_num', 0)  # 连续签到天数
        cpoints = checkin_data.get('cpoints', 0)  # 当前积分
        cexperience = checkin_data.get('cexperience', 0)  # 当前经验值
        cgold = checkin_data.get('cgold', 0)  # 当前金币余额
        cprestige = checkin_data.get('cprestige', 0)  # 声望值
        slogan = checkin_data.get('slogan', '')  # 个性签名
        lottery_type = checkin_data.get('lottery_type', '')  # 抽奖类型
        pre_re_silver = int(checkin_data.get('pre_re_silver', 0))  # 上次获得的银币

        logger.info(f"\n  📅 签到成功!")
        logger.info(f"  " + "="*50)

        # 签到基本信息
        logger.info(f"  📊 签到统计:")
        logger.info(f"    • 连续签到: {daily_num} 天")


        # 账户余额信息
        # 注意：新版账户体系只有「金币 / 碎银子 / 经验」，
        # 旧的 cpoints（积分）字段服务端恒返回 0，且个人中心已无「积分」一项，
        # 因此不再打印，避免每天输出一个误导性的「当前积分: 0」。
        logger.info(f"\n  💰 账户余额:")
        logger.info(f"    • 当前金币: {cgold}")
        logger.info(f"    • 当前经验: {cexperience}")
        logger.info(f"    • 声望值: {cprestige}")
        # 注：cpadd（本次获得积分）新版体系恒为 0，不再展示。

        # 抽奖信息
        if lottery_type:
            logger.info(f"\n  🎰 抽奖信息:")
            logger.info(f"    • 抽奖类型: {lottery_type}")
            if pre_re_silver > 0:
                logger.info(f"    • 上次银币奖励: {pre_re_silver}")

        # 个性签名
        if slogan:
            logger.info(f"\n  💭 个性签名: {slogan}")

        logger.info(f"  " + "="*50)
