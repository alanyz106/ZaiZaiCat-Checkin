#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ZaiZaiCat-Checkin 统一入口

按平台串行执行各签到脚本，单个平台失败/超时不影响其它平台，最后汇总一份结果。

设计要点
--------
1. 失败隔离：每个平台独立子进程 + 超时，异常只记录不中断。
2. 可单独运行：`python3 run_all.py trae` 只跑 Trae，便于调试。
3. 平台注册表：在 PLATFORMS 中增删一项即可维护，无需改动主流程。
4. 跳过随机延迟：统一入口按顺序跑，脚本内置的 0~600 秒随机延迟默认关掉
   （TRAE_JITTER_MAX / WORKBUDDY_JITTER_MAX 置 0），改由本入口控制节奏；
   需要还原错峰行为时传 --keep-jitter。
5. 防并发：flock 文件锁，定时任务与手动执行互斥。
6. 日志：按天写入 logs/checkin-YYYYMMDD.log，同时输出到终端。

用法
----
    python3 run_all.py                 # 跑全部已启用平台
    python3 run_all.py trae workbuddy  # 只跑指定平台
    python3 run_all.py --dry-run       # 所有平台加 --dry-run（仅支持该参数的脚本生效）
    python3 run_all.py --list          # 查看平台注册表
    python3 run_all.py --keep-jitter   # 保留脚本内置随机延迟
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR = PROJECT_ROOT / "logs"
LOCK_PATH = PROJECT_ROOT / ".run_all.lock"
TOKEN_PATH = PROJECT_ROOT / "config" / "token.json"


@dataclass(frozen=True)
class Platform:
    """一个平台的执行定义"""

    key: str                      # 内部标识，也是命令行参数名
    name: str                     # 展示名
    script: str                   # 相对项目根目录的脚本路径
    config_node: Optional[str]    # config/token.json 中对应的节点；None 表示不校验
    env: Dict[str, str] = field(default_factory=dict)   # 额外环境变量
    supports_dry_run: bool = False
    # 单个平台超时（秒）。脚本内置随机延迟已关掉，留足网络重试余量即可
    timeout: int = 900
    # 该平台在汇总通知里保留几行摘要。信息密度高的平台（如 smzdm
    # 有签到/余额/能量值/任务统计多项）可调大
    highlights: int = 4


# ---------------------------------------------------------------------------
# 平台注册表：后续新增平台在这里加一条即可
# ---------------------------------------------------------------------------
PLATFORMS: List[Platform] = [
    Platform(
        key="trae",
        name="Trae CN",
        script="script/trae/main.py",
        config_node="trae",
        env={"TRAE_JITTER_MAX": "0"},
        supports_dry_run=True,
    ),
    Platform(
        key="workbuddy",
        name="WorkBuddy",
        script="script/workbuddy/main.py",
        config_node="workbuddy",
        env={"WORKBUDDY_JITTER_MAX": "0"},
    ),
    # NOTE(2026-10-25): agentrouter 已停用——github_session 生命周期过短
    # （观察到数小时内即失效），每日 cron 必然失败。恢复方法：补回下面这段并更新
    # config/token.json 的 github_session。（原因：token 寿命太短，不适合每日 cron）
    # Platform(
    #     key="agentrouter",
    #     name="Agent Router",
    #     script="script/agentrouter/main.py",
    #     config_node="agentrouter",
    #     supports_dry_run=True,
    #     # 走代理访问，站点对 oauth/state 有按 IP 限流，留足等待重试的时间
    #     timeout=1200,
    # ),
    # ------------------------------------------------------------------
    # 华润系（2026-10-26 抓包接线）
    #
    # 四个子服务在 config/token.json 里共用顶层节点 `huaruntong`，
    # 各自一层子节点，故 config_node 用点号路径表达（见 has_accounts）。
    #
    # 只注册两个：另外两个经用户实测确认「小程序内无签到/答题功能」，
    # 不是抓包失败，脚本保留不跑：
    #   wentiweilaihui — 无签到、无好物兑换
    #   999            — 签到与答题入口均无，疑活动下线
    # ------------------------------------------------------------------
    Platform(
        key="huaruntong_wx",
        name="华润通",
        script="script/huaruntong/huaruntong_wx/main.py",
        config_node="huaruntong.huaruntong_wx",
    ),
    Platform(
        key="ole",
        name="Ole精品超市",
        script="script/huaruntong/ole/main.py",
        config_node="huaruntong.ole",
    ),
    # ------------------------------------------------------------------
    # 什么值得买（2026-10-26 接入）
    #
    # 凭证只有一个全量 Cookie（浏览器 F12 复制），寿命数月，无需续期。
    # 脚本内含纯 Python 实现的请求签名（sign_calculator.py），无需额外依赖；
    # 但 api.py 顶部 import Pillow（处理图片压缩），已装在 vendor/。
    #
    # 签到走 user-api.smzdm.com/checkin，成功返回 error_code="0"。
    # 众测/互动任务常有个别失败（文章已下架、达人关注接口 403），
    # 属上游脚本对接口变动适配不足，不影响签到本身。
    # ------------------------------------------------------------------
    Platform(
        key="smzdm",
        name="什么值得买",
        script="script/smzdm/sign_daily_task/main.py",
        config_node="smzdm",
        # 签到 / 余额 / 能量值 / 必中券 / 众测 / 互动，信息密度高
        highlights=6,
    ),
    # ------------------------------------------------------------------
    # 魔搭 ModelScope 魔粒（2026-10-30 接入）
    #
    # 魔粒是魔搭 API-Inference 的免费计费单位：每天登录发 200，绑定阿里云再发
    # 50，共 250/天，**24 小时过期、不累积**。没有签到按钮，调用一次
    # GET /api/v1/users/login/info 即完成当天签到（服务端幂等），
    # 脚本随后回读魔粒账单确认当日 EARN 记录确实到账。
    #
    # 令牌门槛（踩过）：魔搭建令牌分「只读 / 下载 / 全部」三档，只读或下载档
    # 打魔粒接口会 403 OperationNotAllowed，但打推理 /v1/models 仍是 200，
    # 极易误判成"令牌失效"。必须用「全部」档。国内站与国际站令牌互不通用。
    # ------------------------------------------------------------------
    Platform(
        key="modelscope",
        name="魔搭 ModelScope",
        script="script/modelscope/main.py",
        config_node="modelscope",
        # 签到结果 + 到账魔粒 + 余额 + 今日用量
        highlights=5,
    ),
]

PLATFORM_MAP = {p.key: p for p in PLATFORMS}


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def load_config() -> Dict:
    """读取 config/token.json，不存在或解析失败时返回空字典（由各平台脚本自行报错）"""
    if not TOKEN_PATH.exists():
        return {}
    try:
        return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def has_accounts(config: Dict, node: Optional[str]) -> bool:
    """
    判断配置中该节点是否至少有一个账号

    node 支持用点号表示嵌套路径，例如：
      "workbuddy"               → config["workbuddy"]["accounts"]
      "huaruntong.huaruntong_wx" → config["huaruntong"]["huaruntong_wx"]["accounts"]
    华润系的四个子服务共用 huaruntong 顶层节点、各自一层子节点，
    用嵌套路径就不必为它们把 token.json 拍平。
    """
    if not node:
        return True
    node_conf: Any = config
    for part in node.split("."):
        if not isinstance(node_conf, dict):
            return False
        node_conf = node_conf.get(part)
    if not isinstance(node_conf, dict):
        return False
    accounts = node_conf.get("accounts")
    return isinstance(accounts, list) and len(accounts) > 0


def build_env(platform: Platform, keep_jitter: bool) -> Dict[str, str]:
    """构造子进程环境变量"""
    env = os.environ.copy()
    env.setdefault("TZ", "Asia/Shanghai")
    env["PYTHONUNBUFFERED"] = "1"
    # 确保脚本能 import 项目根目录的 notification.py
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    if not keep_jitter:
        env.update(platform.env)
    # 让子脚本不再各自推送，由本入口统一汇总发一条
    env["CHECKIN_NOTIFY_SILENT"] = "1"
    return env


class Tee:
    """同时写入终端与日志文件"""

    def __init__(self, path: Path):
        self.file = open(path, "a", encoding="utf-8")

    def write(self, text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()
        self.file.write(text)
        self.file.flush()

    def close(self) -> None:
        self.file.close()


def run_platform(platform: Platform, dry_run: bool, keep_jitter: bool,
                 tee: Tee) -> Dict:
    """执行单个平台，返回结果字典"""
    script_path = PROJECT_ROOT / platform.script
    result = {
        "key": platform.key,
        "name": platform.name,
        "status": "unknown",
        "exit_code": None,
        "duration": 0.0,
        "detail": "",
    }

    if not script_path.exists():
        result.update(status="missing", detail=f"脚本不存在: {platform.script}")
        tee.write(f"\n{'=' * 70}\n[{platform.name}] ⚠️ 跳过：{result['detail']}\n")
        return result

    cmd = [sys.executable, str(script_path)]
    if dry_run and platform.supports_dry_run:
        cmd.append("--dry-run")

    header = f"\n{'=' * 70}\n[{platform.name}] 开始 {datetime.now():%Y-%m-%d %H:%M:%S}\n"
    header += f"[{platform.name}] 命令: {' '.join(cmd)}\n{'=' * 70}\n"
    tee.write(header)

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(PROJECT_ROOT),
            env=build_env(platform, keep_jitter),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=platform.timeout,
            text=True,
            errors="replace",
        )
        result["exit_code"] = proc.returncode
        result["output"] = proc.stdout or ""
        if proc.stdout:
            tee.write(proc.stdout if proc.stdout.endswith("\n") else proc.stdout + "\n")
        result["status"] = "ok" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            result["detail"] = f"退出码 {proc.returncode}"
    except subprocess.TimeoutExpired as exc:
        result["status"] = "timeout"
        result["detail"] = f"超时（>{platform.timeout}s）"
        if exc.stdout:
            output = exc.stdout
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            result["output"] = output
            tee.write(output)
            if not output.endswith("\n"):
                tee.write("\n")
        tee.write(f"[{platform.name}] ⏱️ {result['detail']}\n")
    except Exception as exc:  # noqa: BLE001 - 统一兜底，保证不影响其它平台
        result["status"] = "error"
        result["detail"] = f"{exc.__class__.__name__}: {exc}"
        tee.write(f"[{platform.name}] ❌ 执行异常: {result['detail']}\n")

    result["duration"] = time.monotonic() - start
    icon = {"ok": "✅", "failed": "❌", "timeout": "⏱️", "error": "❌"}.get(result["status"], "❔")
    tee.write(f"\n[{platform.name}] {icon} 结束：{result['status']} "
              f"（{result['duration']:.1f}s）{result['detail']}\n")

    # 从子进程输出中提取关键行，供汇总通知使用
    result["highlights"] = extract_highlights(result.pop("output", ""), limit=platform.highlights)
    return result


# 从各平台输出中抓取"值得推送"的行。各脚本日志前缀形如
# "2026-10-25 09:10:01 - __main__ - INFO - xxx"，这里按关键词筛选并去掉前缀。
HIGHLIGHT_KEYWORDS = (
    "签到成功", "今日已签到", "已签到", "获得积分", "奖励积分", "连签", "积分",
    "余额", "凭证", "令牌", "失效", "失败", "过期", "错误", "❌", "⚠️", "🎁",
    # 什么值得买：能量值与必中券是攒实物奖品的唯一路径，必须推送
    "能量", "必中券", "金币", "碎银子", "经验",
    # 魔搭：魔粒与到账金额是唯一有价值的信息
    "魔粒", "可用魔粒",
)
# 这些关键词所在的行优先占推送名额（数值型、结论型信息）
HIGHLIGHT_PRIORITY = (
    "能量", "必中券", "金币", "碎银子", "余额", "签到成功", "今日已签到",
    "连签", "失效", "过期", "魔粒",
)
# 这些行属于噪音，不值得推送
HIGHLIGHT_EXCLUDE = (
    "=====", "开始处理账号", "执行结果统计", "状态正常", "查询签到状态",
    "通过 GitHub OAuth", "站点:", "代理:", "成功加载", "RSA", "公钥",
    # 过程性日志：只是"正在获取/成功获取"，没有信息量
    "正在获取", "成功获取", "开始执行", "正在执行", "开始处理",
)


def extract_highlights(output: str, limit: int = 4) -> List[str]:
    """
    从平台脚本输出里提取摘要行

    Args:
        output (str): 子进程完整输出
        limit (int): 每个平台最多保留几行

    Returns:
        List[str]: 已去重、去掉日志前缀的关键行
    """
    if not output:
        return []

    def _clean(raw: str) -> str:
        """去掉日志前缀，返回纯内容"""
        line = raw.strip()
        m = re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} - (?:[\w.]+ - )?[A-Z]+ - (.*)$", line)
        if m:
            return m.group(1).strip()
        # smzdm 等脚本用 "时间 | LEVEL | 内容" 的格式
        m = re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \| \w+\s*\| (.*)$", line)
        if m:
            return m.group(1).strip()
        return line

    # 按优先级分档收集：高优先级的行先占名额，避免被前面的噪音挤掉。
    # 例：smzdm 的「能量值」出现在日志很靠后，若单纯按顺序截断就会丢失，
    # 而这恰恰是用户最关心的信息。
    tiers: List[List[str]] = [[], [], []]
    seen = set()
    for raw in output.splitlines():
        line = raw.strip()
        if not line or any(x in line for x in HIGHLIGHT_EXCLUDE):
            continue
        if not any(k in line for k in HIGHLIGHT_KEYWORDS):
            continue
        line = _clean(line)
        # 跳过纯统计数字行（由汇总自己算）
        if line.startswith(("总账号数:", "签到成功:", "签到失败:", "今日已签:")):
            continue
        # 跳过只有标题没有数值的行（如「💰 账户余额:」）
        if line.endswith((":", "：")):
            continue
        # 去掉开头的项目符号/emoji，做「语义去重」：
        # "✅ 每日签到成功!" 与 "📅 签到成功!" 内容重复，只留前者
        key = re.sub(r"^[^\w\u4e00-\u9fff]+", "", line)
        key = key.replace("每日", "").replace("当前", "").strip()
        if key in seen:
            continue
        seen.add(key)
        tier = 0 if any(k in line for k in HIGHLIGHT_PRIORITY) else 1
        # 去掉行首项目符号「• 」等，推送里显得干净
        line = re.sub(r"^[•·\-]\s*", "", line)
        tiers[tier].append(line[:80])

    picked: List[str] = []
    for tier in tiers:
        for line in tier:
            if len(picked) >= limit:
                return picked
            picked.append(line)
    return picked


def collect_balance_lines(results: List[Dict], tee: Tee) -> List[str]:
    """
    收集各平台余额，格式化成推送文本行

    余额属于附加信息，任何异常都只记日志、不抛出——绝不能因为查余额失败
    而影响签到汇总推送本身。

    Args:
        results (List[Dict]): run_platform 的结果列表（用其中实际跑过的平台）
        tee (Tee): 日志输出

    Returns:
        List[str]: 形如 "Trae CN: 💰 余额 584.86 / 600 积分" 的行
    """
    # 只查本次实际运行的平台；跳过 missing（脚本不存在）的
    keys = [r["key"] for r in results if r.get("status") != "missing"]
    names = {r["key"]: r["name"] for r in results}
    if not keys:
        return []

    try:
        from balance import BALANCE_FETCHERS, format_balance_line
    except Exception as exc:  # noqa: BLE001
        tee.write(f"\n⚠️ 余额模块不可用，跳过余额查询: {exc}\n")
        return []

    lines: List[str] = []
    for key in keys:
        if key not in BALANCE_FETCHERS:
            continue
        try:
            data = BALANCE_FETCHERS[key]()
            line = format_balance_line(key, data)
        except Exception as exc:  # noqa: BLE001
            tee.write(f"\n⚠️ {names.get(key, key)} 余额查询异常: {exc}\n")
            continue
        if line:
            lines.append(f"{names.get(key, key)}: {line}")
            tee.write(f"\n{names.get(key, key)} 余额: {line}\n")
        else:
            tee.write(f"\nℹ️ {names.get(key, key)} 余额查询无结果（已跳过，不影响签到）\n")
    return lines


def send_summary_notification(results: List[Dict], elapsed: float,
                              started: datetime, dry_run: bool,
                              tee: Tee, include_balance: bool = True) -> None:
    """
    发送统一汇总通知

    优先级：成功/失败都要推，方便确认每日任务确实跑过。
    通知渠道复用项目 notification.py（当前配置为 WxPusher）。
    """
    total = len(results)
    ok = sum(1 for r in results if r["status"] == "ok")
    bad = total - ok
    failed_names = [r["name"] for r in results if r["status"] not in ("ok", "skipped")]

    if bad == 0:
        status_icon, status_text = "✅", "全部成功"
    else:
        status_icon = "⚠️" if ok > 0 else "❌"
        status_text = f"{bad} 个平台异常"

    title = f"{status_icon} 签到 {status_text}（{ok}/{total}）"
    if dry_run:
        title = "[dry-run] " + title

    icons = {"ok": "✅", "failed": "❌", "timeout": "⏱️", "error": "❌",
             "skipped": "⏭️", "missing": "⚠️"}
    lines = [f"📅 {started:%Y-%m-%d %H:%M}  耗时 {elapsed:.0f}s", ""]
    for r in results:
        icon = icons.get(r["status"], "❔")
        duration = f" ({r['duration']:.0f}s)" if r["duration"] else ""
        detail = f" - {r['detail']}" if r["detail"] else ""
        lines.append(f"{icon} {r['name']}{duration}{detail}")
        for hl in r.get("highlights") or []:
            lines.append(f"    {hl}")

    # 余额区块：签到之外最关心的信息，单独成段便于扫读。
    # 全部查询失败时整段省略，不显示空标题。
    balance_lines = collect_balance_lines(results, tee) if include_balance else []
    if balance_lines:
        lines.extend(["", "———— 余额 ————"])
        lines.extend(balance_lines)

    if failed_names:
        lines.extend(["", f"❗ 需处理: {', '.join(failed_names)}"])

    content = "\n".join(lines)

    # 延迟导入：即使通知模块缺失也不影响签到主流程
    try:
        from notification import send_notification
        send_notification(title=title, content=content)
        tee.write(f"\n📬 汇总通知已发送: {title}\n")
    except Exception as exc:  # noqa: BLE001
        tee.write(f"\n⚠️ 汇总通知发送失败（不影响签到结果）: {exc}\n")


def acquire_lock():
    """防并发：拿不到锁说明已有任务在跑"""
    lock_file = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_file.close()
        return None
    return lock_file


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ZaiZaiCat-Checkin 统一签到入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例:\n  python3 run_all.py\n  python3 run_all.py trae workbuddy\n"
               "  python3 run_all.py --dry-run\n  python3 run_all.py --list",
    )
    parser.add_argument("platforms", nargs="*", metavar="platform",
                        help=f"要执行的平台（可选 {', '.join(p.key for p in PLATFORMS)}），留空表示全部")
    parser.add_argument("--dry-run", action="store_true", help="仅校验，不实际签到")
    parser.add_argument("--keep-jitter", action="store_true", help="保留脚本内置的随机延迟")
    parser.add_argument("--list", action="store_true", help="列出平台注册表后退出")
    parser.add_argument("--no-lock", action="store_true", help="跳过文件锁（调试用）")
    parser.add_argument("--no-notify", action="store_true", help="不发送汇总通知")
    parser.add_argument("--no-balance", action="store_true", help="汇总里不附带余额查询")
    args = parser.parse_args()

    # nargs="*" 时 "$@" 为空可能传入空字符串（如 shell 包装脚本），这里过滤并校验
    args.platforms = [p for p in args.platforms if p]
    invalid = [p for p in args.platforms if p not in PLATFORM_MAP]
    if invalid:
        parser.error(f"未知平台: {', '.join(invalid)}；"
                     f"可选: {', '.join(PLATFORM_MAP)}")

    if args.list:
        print(f"{'key':<14}{'name':<18}{'config节点':<16}{'超时':<8}脚本")
        for p in PLATFORMS:
            print(f"{p.key:<14}{p.name:<18}{str(p.config_node):<16}{p.timeout:<8}{p.script}")
        return 0

    # 选择平台
    if args.platforms:
        selected = [PLATFORM_MAP[k] for k in args.platforms]
    else:
        selected = list(PLATFORMS)

    # 锁
    lock_file = None
    if not args.no_lock:
        lock_file = acquire_lock()
        if lock_file is None:
            print("[run_all] 已有签到任务在运行，本次跳过", flush=True)
            return 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"checkin-{datetime.now():%Y%m%d}.log"
    tee = Tee(log_path)

    started = datetime.now()
    tee.write(f"\n{'#' * 70}\n"
              f"# ZaiZaiCat-Checkin 统一执行  {started:%Y-%m-%d %H:%M:%S}\n"
              f"# 平台: {', '.join(p.key for p in selected)}"
              f"{'  [dry-run]' if args.dry_run else ''}\n"
              f"{'#' * 70}\n")

    config = load_config()
    results: List[Dict] = []

    for platform in selected:
        if not has_accounts(config, platform.config_node):
            node = platform.config_node
            results.append({
                "key": platform.key, "name": platform.name, "status": "skipped",
                "exit_code": None, "duration": 0.0,
                "detail": f"config/token.json 中无 {node} 账号",
                "highlights": [],
            })
            tee.write(f"\n{'=' * 70}\n[{platform.name}] ⏭️ 跳过：配置中无 "
                      f"「{node}.accounts」\n")
            continue
        results.append(run_platform(platform, args.dry_run, args.keep_jitter, tee))

    # 汇总
    elapsed = (datetime.now() - started).total_seconds()
    ok = sum(1 for r in results if r["status"] == "ok")
    bad = len(results) - ok
    tee.write(f"\n{'#' * 70}\n# 执行汇总  耗时 {elapsed:.1f}s\n{'#' * 70}\n")
    for r in results:
        icon = {"ok": "✅", "failed": "❌", "timeout": "⏱️",
                "error": "❌", "skipped": "⏭️", "missing": "⚠️"}.get(r["status"], "❔")
        tee.write(f"{icon} {r['name']:<16}{r['status']:<10}"
                  f"{r['duration']:>7.1f}s  {r['detail']}\n")
    tee.write(f"\n总计 {len(results)} 个平台：成功 {ok}，未成功 {bad}\n")
    tee.write(f"日志: {log_path}\n{'#' * 70}\n")

    # 统一通知：一条消息汇总全部平台
    if not args.no_notify:
        send_summary_notification(results, elapsed, started, args.dry_run, tee,
                                  include_balance=not args.no_balance)

    tee.close()
    if lock_file:
        lock_file.close()

    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
