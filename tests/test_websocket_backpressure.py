from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
import pytest
from starlette.testclient import WebSocketDenialResponse
from starlette.websockets import WebSocketDisconnect

from apps.api.middleware_core import database_busy_handler
from apps.api.websocket_backpressure import close_for_backpressure
from packages.core.database_runtime import DatabaseBusyError


@pytest.mark.parametrize("database,accept_first", [(False, False), (True, False), (True, True)])
def test_websocket_wire_backpressure_is_retryable(database, accept_first):
    app = FastAPI()

    @app.websocket("/overloaded")
    async def overloaded(websocket: WebSocket):
        if accept_first:
            await websocket.accept()
        if database:
            await database_busy_handler(websocket, DatabaseBusyError("busy"))
        else:
            await close_for_backpressure(websocket, code="worker_auth_busy", message="Retry shortly")

    with TestClient(app) as client:
        if accept_first:
            with client.websocket_connect("/overloaded") as websocket:
                with pytest.raises(WebSocketDisconnect) as rejected:
                    websocket.receive_json()
                assert rejected.value.code == 1013
        else:
            with pytest.raises(WebSocketDenialResponse) as rejected:
                with client.websocket_connect("/overloaded"):
                    pytest.fail("authentication overload must reject the handshake")
            assert rejected.value.status_code == 503
            assert rejected.value.headers["Retry-After"] == "2"
            assert rejected.value.json()["detail"]["code"] == ("database_busy" if database else "worker_auth_busy")


def test_websocket_without_denial_extension_accepts_then_closes_1013():
    app = FastAPI()

    @app.websocket("/overloaded")
    async def overloaded(websocket: WebSocket):
        await close_for_backpressure(websocket, code="worker_auth_busy", message="Retry shortly")

    async def without_extension(scope, receive, send):
        scope = {**scope, "extensions": {}}
        await app(scope, receive, send)

    with TestClient(without_extension) as client:
        with client.websocket_connect("/overloaded") as websocket:
            with pytest.raises(WebSocketDisconnect) as rejected:
                websocket.receive_json()
            assert rejected.value.code == 1013
