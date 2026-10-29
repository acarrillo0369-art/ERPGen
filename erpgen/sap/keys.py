"""Canonical id <-> SAP key mapping (shared by the exporter and the reconciliation)."""
from __future__ import annotations

from ..config import Config


class KeyMap:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._cust_off = cfg.sap.customer_number_offset
        self._mat_off = cfg.sap.material_number_offset

    def kunnr(self, cid: str) -> str:            # C0001 -> 0000100001
        return f"{int(cid[1:]) + self._cust_off:010d}"

    def cid(self, kunnr: str) -> str:
        return f"C{int(kunnr) - self._cust_off:04d}"

    def matnr(self, mid: str) -> str:            # M0001 -> 000000000000200001
        return f"{int(mid[1:]) + self._mat_off:018d}"

    def mid(self, matnr: str) -> str:
        return f"M{int(matnr) - self._mat_off:04d}"

    @staticmethod
    def saknr(acct: str) -> str:                 # 1200000 -> 0001200000
        return acct.zfill(10)

    @staticmethod
    def acct(saknr: str) -> str:
        return saknr.lstrip("0")

    @staticmethod
    def posnr(n: int) -> str:
        return f"{n:06d}"

    def zterm(self, code: str) -> str:
        return self.cfg.sap.terms_keys[code]

    def knumv(self, so_no: str) -> str:
        return f"{int(so_no) + 5_000_000_000:010d}"
