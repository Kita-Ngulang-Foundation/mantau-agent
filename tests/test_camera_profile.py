from mantau_agent.camera.profile import parse_sdp, pick_lowest_bitrate_video_track

TWO_VIDEO_TRACKS_SDP = """v=0
o=- 0 0 IN IP4 192.168.1.42
s=Media Server
c=IN IP4 0.0.0.0
t=0 0
a=control:*
m=video 0 RTP/AVP 96
b=AS:2048
a=rtpmap:96 H264/90000
a=control:trackID=1
m=video 0 RTP/AVP 96
b=AS:512
a=rtpmap:96 H264/90000
a=control:trackID=2
m=audio 0 RTP/AVP 97
a=rtpmap:97 PCMU/8000
a=control:trackID=3
"""

NO_BANDWIDTH_SDP = """v=0
m=video 0 RTP/AVP 96
a=control:trackID=1
m=video 0 RTP/AVP 96
a=control:trackID=2
"""


def test_parse_sdp_finds_all_media_blocks():
    tracks = parse_sdp(TWO_VIDEO_TRACKS_SDP)
    assert [t.media_type for t in tracks] == ["video", "video", "audio"]


def test_parse_sdp_attaches_bandwidth_and_control_to_the_right_track():
    tracks = parse_sdp(TWO_VIDEO_TRACKS_SDP)
    assert tracks[0].bandwidth_kbps == 2048
    assert tracks[0].control == "trackID=1"
    assert tracks[1].bandwidth_kbps == 512
    assert tracks[1].control == "trackID=2"
    assert tracks[2].bandwidth_kbps is None  # audio track never declared b=AS


def test_pick_lowest_bitrate_video_track_ignores_audio():
    tracks = parse_sdp(TWO_VIDEO_TRACKS_SDP)
    chosen = pick_lowest_bitrate_video_track(tracks)
    assert chosen.control == "trackID=2"
    assert chosen.bandwidth_kbps == 512


def test_pick_lowest_bitrate_falls_back_to_first_when_no_bandwidth_declared():
    tracks = parse_sdp(NO_BANDWIDTH_SDP)
    chosen = pick_lowest_bitrate_video_track(tracks)
    assert chosen.control == "trackID=1"


def test_pick_lowest_bitrate_none_when_no_video_tracks():
    tracks = parse_sdp("v=0\nm=audio 0 RTP/AVP 97\n")
    assert pick_lowest_bitrate_video_track(tracks) is None


def test_parse_sdp_empty_input_returns_no_tracks():
    assert parse_sdp("") == []
