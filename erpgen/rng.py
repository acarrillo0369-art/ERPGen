"""Named, order-independent random streams.

Each component asks for its own stream by name. The stream depends only on
(seed, name), so adding a new component or scenario never shifts the random
numbers consumed by existing ones.
"""
from __future__ import annotations

import zlib

import numpy as np


class Streams:
    def __init__(self, seed: int):
        self.seed = seed
        self._cache: dict[str, np.random.Generator] = {}

    def get(self, name: str) -> np.random.Generator:
        if name not in self._cache:
            ss = np.random.SeedSequence(entropy=self.seed, spawn_key=(zlib.crc32(name.encode()),))
            self._cache[name] = np.random.Generator(np.random.PCG64(ss))
        return self._cache[name]
