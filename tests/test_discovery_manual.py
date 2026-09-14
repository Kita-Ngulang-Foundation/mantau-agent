from mantau_core.contracts import StreamProfile

from mantau_agent.discovery.manual import manual_camera


def test_manual_camera_with_credentials_and_sub_stream():
    camera = manual_camera(camera_id="cam-1", name="Kamar Ibu", host="192.168.1.42",
                            sub_path="/stream2", username="admin", password="admin123")
    assert camera.stream_url(StreamProfile.SUB) == "rtsp://admin:admin123@192.168.1.42:554/stream2"


def test_manual_camera_without_credentials_falls_back_to_main():
    camera = manual_camera(camera_id="cam-1", name="Teras", host="192.168.1.50")
    assert camera.credentials is None
    assert camera.stream_url(StreamProfile.SUB) == "rtsp://192.168.1.50:554/stream1"
