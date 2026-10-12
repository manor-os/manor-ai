"""Temporary overload must not become an HTTP 403 during WS authentication."""
from fastapi import WebSocket
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketState


async def close_for_backpressure(websocket: WebSocket, *, code: str, message: str) -> None:
    if websocket.application_state == WebSocketState.DISCONNECTED:
        return
    if websocket.application_state == WebSocketState.CONNECTING:
        if "websocket.http.response" in websocket.scope.get("extensions", {}):
            await websocket.send_denial_response(JSONResponse(
                status_code=503,
                content={"detail": {"code": code, "message": message}},
                headers={"Retry-After": "2"},
            ))
            return
        # Without the ASGI denial extension, close-before-accept becomes 403.
        # Accept and immediately close; no application/session registration or
        # message dispatch happens on this path.
        await websocket.accept()
    await websocket.close(code=1013, reason=message)
