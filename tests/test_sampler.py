from mantau_agent.detect.sampler import FrameSampler


def test_default_sampler_keeps_every_frame():
    sampler = FrameSampler()
    kept = [sampler.should_keep(ts) for ts in range(0, 500, 33)]
    assert all(kept)


def test_keep_every_n_skips_correctly():
    sampler = FrameSampler(keep_every_n=3)
    # frames 1..9 -> keep on frame_count 3, 6, 9 (1-indexed internally)
    kept = [sampler.should_keep(ts) for ts in range(9)]
    assert kept == [False, False, True, False, False, True, False, False, True]


def test_max_fps_throttles_by_wall_clock_gap():
    sampler = FrameSampler(max_fps=10.0)  # min 100ms between kept frames
    assert sampler.should_keep(0) is True      # first frame always kept
    assert sampler.should_keep(50) is False    # only 50ms since last kept
    assert sampler.should_keep(99) is False    # still under 100ms
    assert sampler.should_keep(100) is True    # exactly the threshold
    assert sampler.should_keep(150) is False   # only 50ms since the last KEPT frame (100)
    assert sampler.should_keep(210) is True    # 110ms since the last kept frame


def test_keep_every_n_and_max_fps_compose():
    sampler = FrameSampler(keep_every_n=2, max_fps=10.0)
    # frame 1 (ts=0): fails keep_every_n (count=1, 1%2!=0)
    # frame 2 (ts=10): passes keep_every_n (count=2), passes max_fps (first kept)
    # frame 3 (ts=20): fails keep_every_n (count=3)
    # frame 4 (ts=30): passes keep_every_n (count=4), but only 20ms since ts=10 -> fails max_fps
    results = [sampler.should_keep(ts) for ts in (0, 10, 20, 30)]
    assert results == [False, True, False, False]
