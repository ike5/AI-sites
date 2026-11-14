"""Streaming change feed (server-sent events): the same rows as GET /changes, pushed instead of polled."""

import asyncio
import json
import threading
from typing import Any, AsyncIterator, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .errors import SiteError

# Seconds, except the limits. One place to tune; tests replace values here.
SETTINGS = {
    "poll": 1.0,            # how often a waiting connection re-reads the feed
    "heartbeat": 15.0,      # a ": keep-alive" comment after this much quiet, so proxies keep the connection
    "max_seconds": 600.0,   # then close with a retry hint so clients reconnect
    "retry_ms": 3000,       # reconnect delay suggested to clients when the connection is closed
    "per_address": 5,       # simultaneous connections from one address
    "total": 100,           # simultaneous connections on this site
}


async def wait(seconds: float) -> None:
    """The only place the stream sleeps. Tests replace it to run a whole connection instantly."""
    await asyncio.sleep(seconds)


def _event(row: dict) -> str:
    return f"id: {row['id']}\nevent: change\ndata: {json.dumps(row, separators=(',', ':'))}\n\n"


def routes(app: Any, site: Any, ctx: Any) -> None:
    lock = threading.Lock()
    open_by_address: dict = {}

    def address_of(request: Request) -> str:
        # Same rule as actions.py: the forwarded address is believed only behind the owner's own proxy.
        address = request.client.host if request.client else "unknown"
        if site.limits["trust_proxy"] and request.headers.get("x-forwarded-for"):
            address = request.headers["x-forwarded-for"].split(",")[0].strip()
        return address

    def take_slot(address: str) -> None:
        with lock:
            if sum(open_by_address.values()) >= SETTINGS["total"] or \
                    open_by_address.get(address, 0) >= SETTINGS["per_address"]:
                err = SiteError("too_many_streams", "Too many change streams are open.",
                                "Close one, or poll GET /changes?since= instead. Retry after the time in Retry-After.",
                                status=429)
                err.retry_after = 5
                raise err
            open_by_address[address] = open_by_address.get(address, 0) + 1

    def make_release(address: str):
        done = []

        def release() -> None:  # safe to call twice: the generator and the response both call it
            with lock:
                if done:
                    return
                done.append(1)
                left = open_by_address.get(address, 1) - 1
                if left > 0:
                    open_by_address[address] = left
                else:
                    open_by_address.pop(address, None)
        return release

    def end_of_feed() -> int:
        site.tick()
        row = site.store.one("SELECT MAX(id) AS m FROM changes")
        return (row["m"] or 0) if row else 0

    async def frames(request: Request, first: dict, path: Optional[str], release) -> AsyncIterator[str]:
        elapsed = quiet = 0.0
        page = first
        try:
            while True:
                for row in page["changes"]:
                    if path is None or row["path"].startswith(path):
                        yield _event(row)
                        quiet = 0.0
                cursor = page["cursor"]
                if page["more"]:
                    page = await run_in_threadpool(site.changes, cursor)
                    continue
                if elapsed >= SETTINGS["max_seconds"]:
                    yield f"retry: {SETTINGS['retry_ms']}\n\n"
                    return
                if await request.is_disconnected():
                    return
                await wait(SETTINGS["poll"])
                elapsed += SETTINGS["poll"]
                quiet += SETTINGS["poll"]
                # site.changes() ticks the clock, so scheduled starts and expiries surface here too.
                page = await run_in_threadpool(site.changes, cursor)
                if not page["changes"] and quiet >= SETTINGS["heartbeat"]:
                    yield ": keep-alive\n\n"
                    quiet = 0.0
        finally:
            release()

    @app.get("/changes/stream")
    async def change_stream(request: Request):
        address = address_of(request)
        take_slot(address)
        release = make_release(address)
        try:
            given = request.query_params.get("since")
            if given is None:
                given = request.headers.get("last-event-id")
            cursor = given if given not in (None, "") else await run_in_threadpool(end_of_feed)
            first = await run_in_threadpool(site.changes, cursor)  # a bad cursor fails here, as a normal 400
            return StreamingResponse(
                frames(request, first, request.query_params.get("path"), release),
                media_type="text/event-stream", background=BackgroundTask(release),
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                         "Link": f'<{ctx.prefix(request)}/.well-known/aisite.json>; rel="describedby"'})
        except BaseException:
            release()
            raise
