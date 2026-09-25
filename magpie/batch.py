"""Submits queued Claude requests as Message Batches (50% cheaper) and completes them.

New screenshots aren't urgent in a save-for-later app, so by default their Claude step is
queued (see AnalyzerRouter / Deferred). Every poll interval this worker sends everything
queued as one batch, checks open batches, and hands finished results to the pipeline.
State lives in the batch_jobs table, so a restart picks up where it left off.
"""

import asyncio
import json
import logging

from .db import Database

log = logging.getLogger(__name__)


class BatchWorker:
    def __init__(self, db: Database, pipeline, client, poll_seconds: int = 60):
        self.db, self.pipeline, self.client, self.poll_seconds = db, pipeline, client, poll_seconds

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Batch worker tick failed; will retry")
            await asyncio.sleep(self.poll_seconds)

    async def tick(self) -> None:
        await self.submit()
        await self.collect()

    async def submit(self) -> None:
        jobs = self.db.unsubmitted_jobs()
        if not jobs:
            return
        batch = await self.client.messages.batches.create(requests=[
            {"custom_id": f"job-{j['id']}", "params": json.loads(j["params"])} for j in jobs
        ])
        self.db.mark_submitted([j["id"] for j in jobs], batch.id)
        log.info("Submitted batch %s with %d screenshot(s)", batch.id, len(jobs))

    async def collect(self) -> None:
        for batch_id in self.db.open_batches():
            batch = await self.client.messages.batches.retrieve(batch_id)
            if batch.processing_status != "ended":
                continue
            async for entry in await self.client.messages.batches.results(batch_id):
                try:
                    job_id = int(entry.custom_id.removeprefix("job-"))
                except ValueError:
                    continue
                job = self.db.batch_job(job_id)
                if not job:
                    continue
                try:
                    await self.pipeline.complete_batch_job(job, entry.result)
                finally:
                    self.db.delete_batch_job(job_id)
            # Anything from this batch without a result (shouldn't happen): queue it again.
            leftovers = [r[0] for r in self.db.conn.execute("SELECT id FROM batch_jobs WHERE batch_id = ?", (batch_id,))]
            if leftovers:
                with self.db.conn:
                    self.db.conn.executemany("UPDATE batch_jobs SET batch_id = NULL WHERE id = ?", [(i,) for i in leftovers])
            log.info("Batch %s done", batch_id)
