from __future__ import annotations

import socket

from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelforge_windows.local_runtime import (
    LOOPBACK_HOST,
    DesktopOriginGuard,
    bind_local_listener,
)


def test_loopback_listener_reuses_preferred_port_across_launches() -> None:
    seed_listener, preferred_port, _ = bind_local_listener(0)
    seed_listener.close()

    first_listener, first_port, first_used_preferred = bind_local_listener(preferred_port)
    assert first_listener.getsockname()[0] == LOOPBACK_HOST
    assert first_port == preferred_port
    assert first_used_preferred is True
    first_listener.close()

    second_listener, second_port, second_used_preferred = bind_local_listener(preferred_port)
    assert second_port == preferred_port
    assert second_used_preferred is True
    second_listener.close()


def test_loopback_listener_falls_back_when_preferred_port_is_busy() -> None:
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    blocker.bind((LOOPBACK_HOST, 0))
    blocker.listen(1)
    preferred_port = int(blocker.getsockname()[1])

    listener, port, used_preferred = bind_local_listener(preferred_port)
    try:
        assert listener.getsockname()[0] == LOOPBACK_HOST
        assert port != preferred_port
        assert used_preferred is False
    finally:
        listener.close()
        blocker.close()


def test_desktop_origin_guard_blocks_cross_site_api_requests() -> None:
    app = FastAPI()

    @app.post("/api/change")
    def change() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/workbench/")
    def workbench() -> dict[str, bool]:
        return {"ok": True}

    origin = "http://127.0.0.1:47831"
    client = TestClient(DesktopOriginGuard(app, origin))

    assert client.post("/api/change", headers={"Origin": origin}).status_code == 200
    assert client.post("/api/change").status_code == 200
    assert (
        client.post("/api/change", headers={"Origin": "https://attacker.example"}).status_code
        == 403
    )
    assert (
        client.post("/api/change", headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    assert (
        client.get("/workbench/", headers={"Origin": "https://attacker.example"}).status_code
        == 200
    )
