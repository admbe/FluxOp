import uvicorn

from api.config import settings


if __name__ == "__main__":
    # App Service images may provide an incompatible global uvloop build. Use
    # the Python runtime's asyncio loop explicitly for deterministic startup.
    uvicorn.run(
        "api.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        loop="asyncio",
        # Flux does not expose WebSocket endpoints; disabling the optional
        # websocket loader avoids runtime incompatibilities in App Service's
        # preinstalled websocket stack.
        ws="none",
    )
