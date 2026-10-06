"""Общие утилиты. У каждого процесса своя SQLite, чужие базы не читаются."""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("DEMO_DATA_DIR", str(ROOT / "data")))
BASE_PORT = int(os.environ.get("DEMO_PORT", "8000"))
URLS = {name: f"http://127.0.0.1:{BASE_PORT + i}" for i, name in enumerate(["hub", "erp", "wms", "tms"])}


class Store:
    """Одна JSON-запись состояния; транзакции сериализуют изменения в учебной БД."""
    def __init__(self, name, initial):
        DATA.mkdir(parents=True, exist_ok=True)
        self.path = DATA / f"{name}.sqlite3"
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            db.execute("INSERT OR IGNORE INTO state VALUES (1, ?)", (json.dumps(initial),))

    @contextmanager
    def edit(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            yield state
            db.execute("UPDATE state SET body=? WHERE id=1", (json.dumps(state, ensure_ascii=False),))

    def read(self):
        with sqlite3.connect(self.path, timeout=10) as db:
            return json.loads(db.execute("SELECT body FROM state WHERE id=1").fetchone()[0])


PRODUCTS = [
    {"sku": "BOX-01", "name": "Коробки упаковочные", "stock": 500},
    {"sku": "PART-02", "name": "Комплектующие для сборки", "stock": 200},
    {"sku": "PAL-03", "name": "Поддоны складские", "stock": 80},
]


def install_offline_docs(app):
    """Swagger использует вложенные JS/CSS, поэтому /docs работает без интернета."""
    from fastapi.openapi.docs import get_swagger_ui_html
    from fastapi.staticfiles import StaticFiles
    app.mount('/api-assets', StaticFiles(directory=ROOT / 'static' / 'api'), name='api-assets')

    @app.get('/docs', include_in_schema=False)
    def docs():
        return get_swagger_ui_html(openapi_url='/openapi.json', title=app.title,
            swagger_js_url='/api-assets/swagger-ui-bundle.js',
            swagger_css_url='/api-assets/swagger-ui.css',
            swagger_favicon_url='data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg"/>')
