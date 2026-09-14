"""The fallback path: an operator-configured camera, no discovery involved.

Deliberately trivial -- this exists so `onvif.py`'s discovery failing (a
camera that doesn't speak ONVIF, or a network that blocks multicast) is
never a dead end. `main.py` tries ONVIF first and falls back to this.
"""

from __future__ import annotations

from mantau_core.contracts import CameraRef, Credentials, StreamProfile


def manual_camera(
    *,
    camera_id: str,
    name: str,
    host: str,
    port: int = 554,
    main_path: str = "/stream1",
    sub_path: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> CameraRef:
    paths = {StreamProfile.MAIN: main_path}
    if sub_path:
        paths[StreamProfile.SUB] = sub_path
    credentials = Credentials(username=username, password=password or "") if username else None
    return CameraRef(camera_id=camera_id, name=name, host=host, port=port,
                      paths=paths, credentials=credentials)
