# Nexie — AI Coding Agent
# Copyright (c) 2026. All rights reserved.
# 本软件仅供个人学习与研究使用，禁止未经授权的商业用途、分发或修改后闭源。
# Licensed for personal, educational, and non-commercial use only.
"""
Nexie — API预算管控+Token追踪 (能力26)
追踪每次API调用的token消耗和费用，支持限额告警。
"""
import time, threading, logging
from datetime import datetime
from collections import deque

logger = logging.getLogger("Nexie.Budget")

# ── 定价 (USD/1M tokens) ──
PRICING = {
    "deepseek-v4-pro": {"input": 0.55, "output": 2.19},
    "deepseek-chat": {"input": 0.27, "output": 1.10},
    "mimo-v2-omni": {"input": 1.50, "output": 6.00},
}


class BudgetTracker:
    """Token消耗追踪+费用预算"""

    def __init__(self, daily_limit_usd: float = 5.0, monthly_limit_usd: float = 50.0):
        self.daily_limit = daily_limit_usd
        self.monthly_limit = monthly_limit_usd
        self._lock = threading.Lock()
        self._calls: deque = deque(maxlen=500)
        self._daily_tokens = 0
        self._monthly_tokens = 0
        self._daily_cost = 0.0
        self._monthly_cost = 0.0
        self._day = datetime.now().day
        self._month = datetime.now().month
        self._start_time = time.time()

    def record(self, model: str, input_tokens: int, output_tokens: int):
        """记录一次API调用的token消耗"""
        now = datetime.now()
        with self._lock:
            # 日/月重置
            if now.day != self._day:
                self._daily_tokens = 0
                self._daily_cost = 0.0
                self._day = now.day
            if now.month != self._month:
                self._monthly_tokens = 0
                self._monthly_cost = 0.0
                self._month = now.month

            # 计算费用
            prices = PRICING.get(model, {"input": 1.0, "output": 4.0})
            cost = (input_tokens / 1_000_000) * prices["input"] + (output_tokens / 1_000_000) * prices["output"]

            self._daily_tokens += input_tokens + output_tokens
            self._monthly_tokens += input_tokens + output_tokens
            self._daily_cost += cost
            self._monthly_cost += cost

            self._calls.append({
                "time": now.isoformat(), "model": model,
                "input": input_tokens, "output": output_tokens,
                "cost": round(cost, 6),
            })

    def get_stats(self) -> dict:
        """获取当前统计"""
        with self._lock:
            elapsed = time.time() - self._start_time
            return {
                "today": {"tokens": self._daily_tokens, "cost": round(self._daily_cost, 4)},
                "month": {"tokens": self._monthly_tokens, "cost": round(self._monthly_cost, 4)},
                "total_calls": len(self._calls),
                "uptime_seconds": int(elapsed),
                "daily_pct": round(self._daily_cost / self.daily_limit * 100, 1) if self.daily_limit else 0,
                "monthly_pct": round(self._monthly_cost / self.monthly_limit * 100, 1) if self.monthly_limit else 0,
            }

    def is_over_limit(self) -> tuple[bool, str]:
        """检查是否超限。返回 (over, reason)"""
        s = self.get_stats()
        if s["daily_pct"] >= 100:
            return True, f"日预算已用完(${s['today']['cost']:.2f}/${self.daily_limit})"
        if s["monthly_pct"] >= 100:
            return True, f"月预算已用完(${s['month']['cost']:.2f}/${self.monthly_limit})"
        return False, ""

    def get_status_text(self) -> str:
        """生成状态文本"""
        s = self.get_stats()
        calls_per_hour = s["total_calls"] / max(1, s["uptime_seconds"] / 3600)
        return (
            f"💰 今日: ${s['today']['cost']:.3f}/{self.daily_limit} ({s['daily_pct']}%) | "
            f"本月: ${s['month']['cost']:.2f}/{self.monthly_limit} | "
            f"调用: {s['total_calls']}次 ({calls_per_hour:.0f}/h)"
        )


# ── 全局 ──
_tracker: BudgetTracker = None

def get_budget_tracker() -> BudgetTracker:
    global _tracker
    if _tracker is None:
        _tracker = BudgetTracker()
    return _tracker
