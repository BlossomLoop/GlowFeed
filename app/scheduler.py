"""定时调度：后台线程每 20 秒巡检，到点的任务丢进工作线程执行。

next_run 计算为纯函数，便于测试。时间均用本地时区（用户配置的是本地时刻）。
"""
import threading
from datetime import datetime, timedelta, timezone

from . import pipeline, skills_board, sources, store

TICK_SECONDS = 20
TREND_REFRESH_TIMES = ["08:30"]      # GitHub 趋势榜每日定时刷新时刻（本地）
SKILLS_REFRESH_TIMES = ["08:40"]     # 热门 Skill 榜每日刷新时刻（错开趋势榜 08:30）
_next_trend: datetime | None = None  # 下次趋势刷新时间（启动时初始化）
_next_skills: datetime | None = None  # 下次 Skill 榜刷新时间（启动时初始化）


def compute_next_run(schedule_type: str, schedule_value, now: datetime) -> datetime:
    """interval: 分钟数；daily: ["08:00","20:00"] 本地时刻列表。"""
    if schedule_type == "interval":
        minutes = max(5, int(schedule_value))  # 下限 5 分钟，保护源站
        return now + timedelta(minutes=minutes)
    times = sorted(schedule_value) if isinstance(schedule_value, list) else [str(schedule_value)]
    for t in times:
        hh, mm = t.split(":")
        candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if candidate > now:
            return candidate
    hh, mm = times[0].split(":")
    return (now + timedelta(days=1)).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def compute_prev_run(schedule_type: str, schedule_value, now: datetime) -> datetime:
    """最近一次"本应触发"的时刻（<= now）。compute_next_run 的对偶，用于启动补跑判断。"""
    if schedule_type == "interval":
        minutes = max(5, int(schedule_value))
        return now - timedelta(minutes=minutes)
    times = sorted(schedule_value) if isinstance(schedule_value, list) else [str(schedule_value)]
    for t in reversed(times):
        hh, mm = t.split(":")
        candidate = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if candidate <= now:
            return candidate
    hh, mm = times[-1].split(":")
    return (now - timedelta(days=1)).replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def _missed_since_last_run(task: dict, now_local: datetime) -> bool:
    """启动补跑判断：最近一次计划触发时刻是否晚于任务上次实际运行。

    last_run 存的是 UTC（store.now），计划时刻是本地时区，故把本地 prev 换算成 UTC
    再与 last_run 同基准比较（naive.astimezone 默认按本地时区解释）。
    """
    prev_local = compute_prev_run(task["schedule_type"], task["schedule_value"], now_local)
    prev_utc = prev_local.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    last_run = task.get("last_run") or ""
    return last_run < prev_utc


def reschedule_task(task_id: int, now: datetime | None = None) -> None:
    task = store.get_task(task_id)
    if not task:
        return
    if not task["enabled"]:
        store.patch_task(task_id, next_run=None)
        return
    nxt = compute_next_run(task["schedule_type"], task["schedule_value"], now or datetime.now())
    store.patch_task(task_id, next_run=nxt.strftime("%Y-%m-%d %H:%M:%S"))


def _run_and_reschedule(task_id: int) -> None:
    try:
        pipeline.run_task(task_id)
    finally:
        reschedule_task(task_id)


def _tick() -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    due = [t for t in store.list_tasks()
           if t["enabled"] and t.get("next_run") and t["next_run"] <= now]
    for task in due:
        # 先推进 next_run 再执行，避免长任务期间重复触发
        reschedule_task(task["id"])
        threading.Thread(target=_run_and_reschedule, args=(task["id"],), daemon=True).start()

    # GitHub 趋势榜每日定时刷新（与任务调度无关，独立计时）
    global _next_trend
    if _next_trend and datetime.now() >= _next_trend:
        threading.Thread(target=sources.warm_trending, daemon=True).start()
        _next_trend = compute_next_run("daily", TREND_REFRESH_TIMES, datetime.now())

    # 热门 Skill 榜每日定时刷新（独立计时，错开趋势榜）
    global _next_skills
    if _next_skills and datetime.now() >= _next_skills:
        threading.Thread(target=skills_board.warm_skills, daemon=True).start()
        _next_skills = compute_next_run("daily", SKILLS_REFRESH_TIMES, datetime.now())


def start() -> None:
    # 启动时为所有启用任务补齐 next_run（如服务重启导致过期）；
    # 若错过了最近一次计划触发（服务在该时刻未运行），立即补跑一次，避免"今日全站抓取"为 0。
    now_local = datetime.now()
    for task in store.list_tasks():
        if not task["enabled"]:
            continue
        reschedule_task(task["id"])
        if _missed_since_last_run(task, now_local):
            threading.Thread(target=_run_and_reschedule, args=(task["id"],), daemon=True).start()

    # 趋势榜：重启即预热刷新一次，并排定下一个每日 08:30 刷新
    global _next_trend
    _next_trend = compute_next_run("daily", TREND_REFRESH_TIMES, datetime.now())
    threading.Thread(target=sources.warm_trending, daemon=True).start()

    # 热门 Skill 榜：重启即预热刷新一次，并排定下一个每日 08:40 刷新
    global _next_skills
    _next_skills = compute_next_run("daily", SKILLS_REFRESH_TIMES, datetime.now())
    threading.Thread(target=skills_board.warm_skills, daemon=True).start()

    def loop():
        while True:
            try:
                _tick()
            except Exception as e:
                print(f"[scheduler] tick error: {e}")
            threading.Event().wait(TICK_SECONDS)

    threading.Thread(target=loop, daemon=True, name="scheduler").start()
