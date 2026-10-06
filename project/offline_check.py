"""Проверка переносного Python до запуска процессов приложения."""
import sys
import sqlite3
import ssl
import fastapi
import uvicorn
import httpx
import pydantic_core

if __name__ == '__main__':
    assert sys.version_info >= (3, 11), 'Python 3.11+ is required'
    print('LOGOS runtime OK:',sys.version.split()[0],sys.platform)
    print('FastAPI',fastapi.__version__,'SQLite',sqlite3.sqlite_version)
