"""Имитаторы ERP, WMS и TMS с HTTP API и независимыми хранилищами."""
import asyncio
import hashlib
import json
import time
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from services.common import PRODUCTS, Store, install_offline_docs


class Order(BaseModel):
    id: str = Field(min_length=8, max_length=80)
    customer: str = Field(min_length=1, max_length=100)
    sku: str = Field(min_length=1, max_length=30)
    quantity: int = Field(ge=1, le=10000)
    window_start: float = Field(gt=0)
    window_end: float = Field(gt=0)
    picking_min: int = Field(default=30, ge=1, le=240)
    loading_min: int = Field(default=20, ge=1, le=120)
    travel_min: int = Field(default=50, ge=1, le=600)
    buffer_min: int = Field(default=10, ge=0, le=120)

    @model_validator(mode="after")
    def check_window(self):
        if self.window_end <= self.window_start:
            raise ValueError("Конец окна должен быть позже начала")
        if not self.customer.strip():
            raise ValueError("Укажите получателя")
        return self


class Fault(BaseModel):
    delay_ms: int = Field(default=0, ge=0, le=10000)
    fail_next: int = Field(default=0, ge=0, le=100)
    lost_next: int = Field(default=0, ge=0, le=100)


class Action(BaseModel):
    action: Literal["reserve", "ready", "consume", "book", "depart", "arrive", "plan", "complete", "cancel"]
    order_id: str
    data: dict = Field(default_factory=dict)


def make_app(name):
    db = Store(name, {"orders": {}, "reservations": {}, "shipments": {},
                      "products": PRODUCTS, "keys": {}, "cancelled": [],
                      "fault": {"delay_ms": 0, "fail_next": 0, "lost_next": 0}})
    app = FastAPI(title=f"{name.upper()} — учебная система", version="1.1.0", docs_url=None, redoc_url=None)
    install_offline_docs(app)

    @app.middleware("http")
    async def simulate_fault(request: Request, call_next):
        if request.url.path not in ("/commands", "/orders") or request.method != "POST":
            return await call_next(request)
        with db.edit() as s:
            delay = s["fault"]["delay_ms"]
            fail = s["fault"]["fail_next"] > 0
            lost = not fail and s["fault"]["lost_next"] > 0
            if fail:
                s["fault"]["fail_next"] -= 1
            elif lost:
                s["fault"]["lost_next"] -= 1
        if delay:
            await asyncio.sleep(delay / 1000)
        if fail:
            return JSONResponse({"detail": "Имитируется временный отказ"}, status_code=503)
        response = await call_next(request)
        if lost and response.status_code < 300:
            # Команда закоммичена, но подтверждение не дошло: модель сбоя шлюза.
            return JSONResponse({"detail": "Результат сохранён, подтверждение потеряно (демо)"}, status_code=504)
        return response

    @app.get("/health")
    def health():
        return {"service": name, "ok": True}

    @app.get("/snapshot")
    def snapshot():
        s = db.read()
        s.pop("keys")
        return s

    @app.post("/fault")
    def fault(config: Fault):
        with db.edit() as s:
            s["fault"] = config.model_dump()
        return config

    if name == "erp":
        @app.post("/orders")
        def create_order(order: Order):
            payload = order.model_dump()
            with db.edit() as s:
                old = s["orders"].get(order.id)
                if old:
                    if old["payload"] != payload:
                        raise HTTPException(409, "ID уже использован с другими данными")
                    return old
                result = {"payload": payload, "status": "new", "created_at": time.time()}
                s["orders"][order.id] = result
                # Запись new служит устойчивым outbox, пока hub не подтвердит план.
                return result

        @app.get("/outbox")
        def outbox():
            return [o["payload"] for o in db.read()["orders"].values() if o["status"] == "new"]

    @app.post("/commands")
    def command(cmd: Action, idempotency_key: str = Header(min_length=1, max_length=150)):
        fingerprint = hashlib.sha256(json.dumps(cmd.model_dump(), sort_keys=True).encode()).hexdigest()
        with db.edit() as s:
            existing = s["keys"].get(idempotency_key)
            if existing:
                if existing["fingerprint"] != fingerprint:
                    raise HTTPException(409, "Ключ идемпотентности использован с другими данными")
                return existing["result"]
            oid, action, data = cmd.order_id, cmd.action, cmd.data
            if oid in s["cancelled"] and action != "cancel":
                raise HTTPException(409, "Заказ отменён")
            if action == "cancel":
                if name == "wms":
                    r = s["reservations"].get(oid)
                    if r and r["status"] == "consumed":
                        raise HTTPException(409, "Товар уже отгружен: требуется возврат, а не отмена")
                    if r and r["status"] != "cancelled":
                        next(p for p in s["products"] if p["sku"] == r["sku"])["stock"] += r["quantity"]
                        r["status"] = "cancelled"
                if name == "tms" and oid in s["shipments"]:
                    if s["shipments"][oid]["status"] in ("departed", "arrived"):
                        raise HTTPException(409, "Машина уже отправлена")
                    s["shipments"][oid]["status"] = "cancelled"
                if name == "erp" and oid in s["orders"]:
                    s["orders"][oid]["status"] = "cancelled"
                if oid not in s["cancelled"]:
                    s["cancelled"].append(oid)
                result = {"status": "cancelled"}
            elif name == "wms":
                r = s["reservations"].get(oid)
                if action == "reserve":
                    parsed = Order.model_validate(data)
                    product = next((p for p in s["products"] if p["sku"] == parsed.sku), None)
                    if not product:
                        raise HTTPException(422, "Неизвестный товар")
                    if r:
                        if r["sku"] != parsed.sku or r["quantity"] != parsed.quantity:
                            raise HTTPException(409, "Резерв уже существует с другими данными")
                    else:
                        if product["stock"] < parsed.quantity:
                            raise HTTPException(409, "Недостаточно товара на складе")
                        product["stock"] -= parsed.quantity
                        r = {"sku": parsed.sku, "quantity": parsed.quantity, "status": "reserved"}
                        s["reservations"][oid] = r
                elif action in ("ready", "consume"):
                    if not r:
                        raise HTTPException(409, "Нет резерва")
                    expected, target = ("reserved", "ready") if action == "ready" else ("ready", "consumed")
                    if r["status"] not in (expected, target):
                        raise HTTPException(409, "Неверный этап склада")
                    r["status"] = target
                else:
                    raise HTTPException(422, "Неизвестная команда WMS")
                result = dict(r)
            elif name == "tms":
                shipment = s["shipments"].get(oid)
                if action == "book":
                    try:
                        begin, end = float(data["pickup"]), float(data["window_end"])
                    except (KeyError, TypeError, ValueError):
                        raise HTTPException(422, "Требуется интервал перевозки")
                    if end <= begin:
                        raise HTTPException(422, "Некорректный интервал перевозки")
                    if not shipment:
                        vehicle = None
                        for truck in ("А101АА", "В202ВВ", "Е303ЕЕ", "К404КК", "М505ММ"):
                            busy = any(x["vehicle"] == truck and x["status"] != "cancelled"
                                       and begin < x["end"] and end > x["begin"]
                                       for x in s["shipments"].values())
                            if not busy:
                                vehicle = truck
                                break
                        if vehicle is None:
                            raise HTTPException(409, "Нет свободных машин в этом интервале")
                        shipment = {"vehicle": vehicle, "begin": begin, "end": end, "status": "booked"}
                        s["shipments"][oid] = shipment
                elif action in ("depart", "arrive"):
                    if not shipment:
                        raise HTTPException(409, "Перевозка не создана")
                    expected, target = ("booked", "departed") if action == "depart" else ("departed", "arrived")
                    if shipment["status"] not in (expected, target):
                        raise HTTPException(409, "Неверный этап перевозки")
                    shipment["status"] = target
                else:
                    raise HTTPException(422, "Неизвестная команда TMS")
                result = dict(shipment)
            else:
                order = s["orders"].get(oid)
                if not order:
                    raise HTTPException(404, "Заказ не найден")
                if action not in ("plan", "complete"):
                    raise HTTPException(422, "Неизвестная команда ERP")
                order["status"] = "planned" if action == "plan" else "delivered"
                order["integration"] = data
                result = {"status": order["status"]}
            s["keys"][idempotency_key] = {"fingerprint": fingerprint, "result": result}
            return result
    return app
