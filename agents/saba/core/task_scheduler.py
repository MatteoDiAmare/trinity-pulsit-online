"""Async task runner for independent, bounded jobs."""
import asyncio


class TaskScheduler:
    def __init__(self, concurrency=3):
        if concurrency < 1:
            raise ValueError("concurrency must be positive")
        self.semaphore = asyncio.Semaphore(concurrency)

    async def run(self, jobs):
        async def guarded(job):
            async with self.semaphore:
                return await job()
        return await asyncio.gather(*(guarded(job) for job in jobs), return_exceptions=True)
