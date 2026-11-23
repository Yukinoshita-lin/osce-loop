"""本地启动入口：python run.py"""
from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "app.api:app",
        host=os.getenv("OSCELOOP_HOST", "127.0.0.1"),
        port=int(os.getenv("OSCELOOP_PORT", "8000")),
        reload=os.getenv("OSCELOOP_RELOAD", "0") == "1",
    )
