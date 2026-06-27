import os
import logging
import asyncio
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

logging.basicConfig(level=logging.INFO)

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "ok"}

@app.get("/mini-app")
async def mini_app():
    return HTMLResponse("<h1>Mini App test OK</h1>")

async def main():
    config = uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", 3000)), log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main())