"""Оркестратор: durable jobs, HTTP, SLA, backoff и компенсация резервов."""
import asyncio
import contextlib
import random
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from services.common import ROOT, Store, URLS, install_offline_docs
from services.systems import Fault, Order

STEPS = ["reserve", "book", "plan", "ready", "load", "depart", "arrive", "complete"]
CANCEL = ["cancel_tms", "cancel_wms", "cancel_erp"]
TARGET = {"reserve": "wms", "ready": "wms", "load": "wms", "book": "tms",
          "depart": "tms", "arrive": "tms", "plan": "erp", "complete": "erp",
          "cancel_tms": "tms", "cancel_wms": "wms", "cancel_erp": "erp"}
POLICY = {"wms": {"sla": 2.0, "timeout": 5.0}, "tms": {"sla": 2.0, "timeout": 5.0},
          "erp": {"sla": 1.0, "timeout": 3.0}}
db = Store("hub", {"jobs": {}, "logs": [], "clock": time.time(), "running": False,
                    "speed": 60, "worker_error": None})
lock = asyncio.Lock()


def plan_order(order):
    start = order["window_start"]
    depart = start - 60 * (order["travel_min"] + order["buffer_min"])
    ready = depart - 60 * order["loading_min"]
    return {"start": ready - 60 * order["picking_min"], "ready": ready,
            "depart": depart, "arrival": start - 60 * order["buffer_min"], "delivery": start}


def add_job(order):
    with db.edit() as s:
        if order["id"] not in s["jobs"]:
            s["jobs"][order["id"]] = {"order": order, "plan": plan_order(order),
                "stage": "reserve", "state": "pending", "attempts": 0, "next_at": 0,
                "commands": {}, "actual": {}, "error": None, "created_at": time.time(),
                "model_created": s["clock"], "finished_at": None}


def due_time(job):
    stage, p, a, o = job["stage"], job["plan"], job["actual"], job["order"]
    if stage == "ready":
        return max(p["ready"], a["reserve"] + 60 * o["picking_min"])
    if stage == "load":
        return max(p["depart"], a["ready"] + 60 * o["loading_min"])
    if stage == "arrive":
        return a["depart"] + 60 * o["travel_min"]
    if stage == "complete":
        return max(o["window_start"], a["arrive"])
    return 0


def command_for(job, clock):
    stage, order = job["stage"], job["order"]
    action = "cancel" if stage in CANCEL else "consume" if stage == "load" else stage
    data = {}
    if stage == "reserve":
        data = order
    elif stage == "book":
        data = {"pickup": max(job["plan"]["ready"], job["actual"]["reserve"] + order["picking_min"] * 60),
                "window_end": max(order["window_end"], clock + (order["picking_min"] + order["loading_min"] + order["travel_min"]) * 60)}
    elif stage == "plan":
        data = job["plan"]
    elif stage == "complete":
        data = {"delivered_at": clock, "in_window": order["window_start"] <= clock <= order["window_end"]}
    return {"action": action, "order_id": order["id"], "data": data}


async def process_job(client, oid):
    current = db.read()
    job = current["jobs"][oid]
    if job["state"] not in ("pending", "retry") or job["next_at"] > time.time():
        return
    if due_time(job) > current["clock"]:
        return
    stage = job["stage"]
    service = TARGET[stage]
    # Сначала записываем неизменный payload. Повтор после сбоя использует тот же ключ и тело.
    with db.edit() as s:
        j = s["jobs"][oid]
        if stage not in j["commands"]:
            j["commands"][stage] = {"body": command_for(j, s["clock"]), "started_at": time.time()}
        command = j["commands"][stage]["body"]
    started = time.perf_counter()
    code, detail, retryable = 0, "", False
    try:
        response = await asyncio.wait_for(client.post(URLS[service] + "/commands", json=command,
                                     headers={"Idempotency-Key": f"{oid}:{stage}"},
                                     timeout=POLICY[service]["timeout"]), timeout=POLICY[service]["timeout"])
        code = response.status_code
        if code >= 400:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text[:200]
            retryable = code in (408, 429, 500, 502, 503, 504)
    except (httpx.TimeoutException, asyncio.TimeoutError):
        detail, retryable = "Тайм-аут ожидания ответа", True
    except httpx.RequestError:
        detail, retryable = "Сервис недоступен или соединение прервано", True
    elapsed = time.perf_counter() - started
    success = 200 <= code < 300
    with db.edit() as s:
        j = s["jobs"][oid]
        attempt = j["attempts"] + 1
        s["logs"].append({"at": time.time(), "order_id": oid, "stage": stage,
                          "service": service, "attempt": attempt, "code": code,
                          "elapsed_ms": round(elapsed * 1000, 1), "sla_ms": POLICY[service]["sla"] * 1000,
                          "sla_ok": success and elapsed <= POLICY[service]["sla"],
                          "success": success, "detail": detail})
        j["attempts"] = attempt
        if success:
            j["actual"][stage] = command["data"]["delivered_at"] if stage == "complete" else s["clock"]
            j["commands"][stage]["finished_at"] = time.time()
            j["error"], j["attempts"], j["next_at"] = None, 0, 0
            sequence = CANCEL if stage in CANCEL else STEPS
            idx = sequence.index(stage)
            if idx == len(sequence) - 1:
                j["state"] = "cancelled" if stage in CANCEL else "done"
                j["finished_at"] = time.time()
            else:
                j["stage"], j["state"] = sequence[idx + 1], "pending"
        else:
            j["error"] = detail
            if retryable and attempt < 4:
                j["state"] = "retry"
                j["next_at"] = time.time() + 2 ** attempt + random.uniform(0, .3)
            else:
                j["state"] = "dead"


async def worker():
    last_poll = 0
    async with httpx.AsyncClient(trust_env=False) as client:
        while True:
            try:
                async with lock:
                    now = time.monotonic()
                    if now - last_poll > 1:
                        try:
                            r = await client.get(URLS["erp"] + "/outbox", timeout=2)
                            r.raise_for_status()
                            for order in r.json():
                                add_job(order)
                        except httpx.HTTPError:
                            pass  # ERP outbox остаётся в ERP; заберём после восстановления.
                        last_poll = now
                    for oid in list(db.read()["jobs"]):
                        await process_job(client, oid)
                    with db.edit() as s:
                        s["worker_error"] = None
            except Exception as exc:
                with db.edit() as s:
                    s["worker_error"] = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(.15)


async def clock_worker():
    previous = time.monotonic()
    while True:
        await asyncio.sleep(.1)
        now = time.monotonic()
        with db.edit() as s:
            if s["running"]:
                s["clock"] += (now - previous) * s["speed"]
        previous = now


@asynccontextmanager
async def lifespan(app):
    # После рестарта время всегда на паузе; очередь сохраняется и продолжается.
    with db.edit() as s:
        s["running"] = False
    task = asyncio.create_task(worker())
    clock_task = asyncio.create_task(clock_worker())
    yield
    task.cancel()
    clock_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    with contextlib.suppress(asyncio.CancelledError):
        await clock_task


app = FastAPI(title="LOGOS / сервис интеграции", version="1.1.0", lifespan=lifespan, docs_url=None, redoc_url=None)
install_offline_docs(app)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/health")
def health():
    return {"ok": True, "service": "hub"}


@app.get("/api/state")
async def state():
    async def fetch(name, client):
        try:
            r = await client.get(URLS[name] + "/snapshot", timeout=1)
            r.raise_for_status()
            return name, {"online": True, "data": r.json()}
        except httpx.HTTPError:
            return name, {"online": False, "data": None}
    async with httpx.AsyncClient(trust_env=False) as client:
        systems = dict(await asyncio.gather(*(fetch(name, client) for name in ("erp", "wms", "tms"))))
    s = db.read()
    logs = s["logs"]
    count = len(logs)
    delivered = [j for j in s["jobs"].values() if j["state"] == "done"]
    return {"jobs": list(s["jobs"].values()), "logs": logs[-200:][::-1], "systems": systems,
            "clock": s["clock"], "running": s["running"], "speed": s["speed"], "policy": POLICY,
            "worker_error": s["worker_error"], "metrics": {
                "requests": count, "success_pct": round(100 * sum(x["success"] for x in logs) / count, 1) if count else None,
                "sla_pct": round(100 * sum(x["sla_ok"] for x in logs) / count, 1) if count else None,
                "retries": sum(x["attempt"] > 1 for x in logs), "delivered": len(delivered),
                "in_window": sum(j["order"]["window_start"] <= j["actual"]["complete"] <= j["order"]["window_end"] for j in delivered)}}


@app.post("/api/orders")
async def create_order(order: Order):
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            r = await client.post(URLS["erp"] + "/orders", json=order.model_dump(), timeout=4)
        except httpx.RequestError:
            raise HTTPException(503, "Нет подтверждения ERP. Повторите сохранение: ID сохранён, дубликата не будет.")
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.json().get("detail", "Ошибка ERP"))
    add_job(order.model_dump())
    return r.json()


class ClockControl(BaseModel):
    running: bool
    speed: int = Field(default=60, ge=1, le=600)


@app.post("/api/clock")
async def clock(control: ClockControl):
    async with lock:
        with db.edit() as s:
            s["running"], s["speed"] = control.running, control.speed
    return control


@app.post("/api/fault/{service}")
async def fault(service: str, config: Fault):
    if service not in ("erp", "wms", "tms"):
        raise HTTPException(404, "Система не найдена")
    async with httpx.AsyncClient(trust_env=False) as client:
        try:
            r = await client.post(URLS[service] + "/fault", json=config.model_dump(), timeout=2)
            r.raise_for_status()
        except httpx.HTTPError:
            raise HTTPException(503, "Не удалось применить настройку")
    return r.json()


@app.post("/api/jobs/{oid}/retry")
async def retry(oid: str):
    async with lock:
        with db.edit() as s:
            j = s["jobs"].get(oid)
            if not j:
                raise HTTPException(404, "Заказ не найден")
            if j["state"] != "dead":
                raise HTTPException(409, "Повтор вручную доступен только после остановки операции")
            j["state"], j["attempts"], j["next_at"], j["error"] = "pending", 0, 0, None
    return {"ok": True}


@app.post("/api/jobs/{oid}/cancel")
async def cancel(oid: str):
    async with lock:
        with db.edit() as s:
            j = s["jobs"].get(oid)
            if not j:
                raise HTTPException(404, "Заказ не найден")
            if j["stage"] not in STEPS[:4] or j["state"] in ("done", "cancelled"):
                raise HTTPException(409, "Отмена доступна до начала отгрузки")
            j["stage"], j["state"], j["attempts"], j["next_at"], j["error"] = "cancel_tms", "pending", 0, 0, None
    return {"ok": True}
