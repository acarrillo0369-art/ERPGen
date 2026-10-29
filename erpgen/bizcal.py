"""Business calendar and fiscal periods (fiscal year = calendar year, 12 periods)."""
from __future__ import annotations

from datetime import date, timedelta


def _us_holidays(year: int) -> set[date]:
    hol = {date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)}
    d = date(year, 5, 31)                      # Memorial Day: last Monday of May
    while d.weekday() != 0:
        d -= timedelta(days=1)
    hol.add(d)
    d = date(year, 9, 1)                       # Labor Day: first Monday of September
    while d.weekday() != 0:
        d += timedelta(days=1)
    hol.add(d)
    d = date(year, 11, 1)                      # Thanksgiving: fourth Thursday of November
    while d.weekday() != 3:
        d += timedelta(days=1)
    hol.add(d + timedelta(days=21))
    return hol


class BizCalendar:
    def __init__(self, start: date, end: date):
        self.holidays: set[date] = set()
        for y in range(start.year, end.year + 2):
            self.holidays |= _us_holidays(y)

    def is_bd(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def next_bd(self, d: date) -> date:
        """First business day on or after d."""
        while not self.is_bd(d):
            d += timedelta(days=1)
        return d

    def add_bd(self, d: date, n: int) -> date:
        d = self.next_bd(d)
        done = 0
        while done < n:
            d += timedelta(days=1)
            if self.is_bd(d):
                done += 1
        return d

    def business_days(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d <= end:
            if self.is_bd(d):
                out.append(d)
            d += timedelta(days=1)
        return out

    def quarter_push_days(self, start: date, end: date, k: int) -> set[date]:
        """The last k business days of every quarter in [start, end]."""
        out: set[date] = set()
        for y in range(start.year, end.year + 1):
            for qe_month in (3, 6, 9, 12):
                d = self.last_day_of_month(y, qe_month)
                got = 0
                while got < k:
                    if self.is_bd(d):
                        out.add(d)
                        got += 1
                    d -= timedelta(days=1)
        return out

    @staticmethod
    def last_day_of_month(year: int, month: int) -> date:
        return (date(year, month, 28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    @staticmethod
    def period(d: date) -> tuple[int, int]:
        return d.year, d.month

    def period_close_date(self, fy: int, period: int, lag_bd: int) -> date:
        d = self.last_day_of_month(fy, period)
        while not self.is_bd(d):
            d -= timedelta(days=1)
        return self.add_bd(d, lag_bd)
