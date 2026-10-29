"""Authorized UI movement orchestration; no evasion or fingerprint spoofing."""
from saba.motion import generate_path


class BehaviorEngine:
    def __init__(self, pointer, *, seed=42):
        self.pointer = pointer
        self.seed = seed

    async def move(self, start, end, *, duration=0.65):
        points = generate_path(start, end, duration=duration, seed=self.seed)
        for point in points:
            await self.pointer.move(point.x, point.y)
        return points

    async def click(self, x, y):
        await self.pointer.click(x, y)
