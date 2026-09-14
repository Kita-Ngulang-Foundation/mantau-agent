"""Parse a camera's SDP to pick the lower-bitrate stream, for the (less
common but real) camera that exposes multiple video tracks within ONE RTSP
path rather than via separate URLs.

Most cheap consumer IP cameras actually expose main/sub as two different
URL paths (`/stream1` vs `/stream2`) -- that convention is already handled
by `CameraRef.paths`/`stream_url()` in mantau_core and needs no SDP parsing
at all. This module is for cameras that instead offer several `m=video`
lines in one SDP (more common on ONVIF-profile-aware / higher-end
hardware) -- a genuinely different mechanism, not a more-general version of
the same one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MediaTrack:
    media_type: str              # "video" | "audio" | ...
    bandwidth_kbps: int | None = None
    control: str | None = None    # from a=control: -- relative or absolute


def parse_sdp(sdp_text: str) -> list[MediaTrack]:
    """One `MediaTrack` per `m=` line, with its own `b=AS:`/`a=control:`
    lines attached (SDP scopes both to the most recent `m=` line, not the
    whole session, once at least one `m=` line has appeared)."""
    tracks: list[MediaTrack] = []
    current: MediaTrack | None = None
    for raw_line in sdp_text.splitlines():
        line = raw_line.strip()
        if line.startswith("m="):
            if current is not None:
                tracks.append(current)
            media_type = line[2:].split(" ", 1)[0] if line[2:] else ""
            current = MediaTrack(media_type=media_type)
        elif current is not None and line.startswith("b=AS:"):
            try:
                current.bandwidth_kbps = int(line.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif current is not None and line.startswith("a=control:"):
            current.control = line.split(":", 1)[1].strip()
    if current is not None:
        tracks.append(current)
    return tracks


def pick_lowest_bitrate_video_track(tracks: list[MediaTrack]) -> MediaTrack | None:
    """The video track with the lowest declared bandwidth -- the sub stream,
    by definition, when a camera exposes more than one this way.

    Falls back to the FIRST video track when none declare `b=AS:` (many real
    cameras omit it entirely). That fallback is a neutral default, not a
    claim that the first track is the smaller one -- log the chosen track's
    bandwidth (or its absence) rather than assume this always picked right.
    """
    video_tracks = [t for t in tracks if t.media_type == "video"]
    if not video_tracks:
        return None
    with_bandwidth = [t for t in video_tracks if t.bandwidth_kbps is not None]
    if with_bandwidth:
        return min(with_bandwidth, key=lambda t: t.bandwidth_kbps)
    return video_tracks[0]
