"""End-to-end run: world -> simulate -> post -> validate."""
from __future__ import annotations

from dataclasses import dataclass

from .accounting import AccountingEngine
from .bizcal import BizCalendar
from .config import Config
from .model import Event, JournalEntry, MasterData
from .rng import Streams
from .sim import Simulator
from .validate import Report, validate
from .world import build_world


@dataclass
class Result:
    cfg: Config
    md: MasterData
    cal: BizCalendar
    events: list[Event]
    entries: list[JournalEntry]
    report: Report


def generate(cfg: Config) -> Result:
    streams = Streams(cfg.seed)
    cal = BizCalendar(cfg.start_date, cfg.end_date)
    md = build_world(cfg, streams)
    events = Simulator(cfg, md, cal, streams).run()
    entries = AccountingEngine(cfg, md, cal).post(events)
    report = validate(cfg, md, cal, events, entries)
    return Result(cfg, md, cal, events, entries, report)
