from mantau_agent.uplink.seq import SeqCounter


def test_starts_at_zero_for_a_fresh_path(tmp_path):
    counter = SeqCounter(tmp_path / "seq.txt")
    assert counter.next() == 0
    assert counter.next() == 1
    assert counter.next() == 2


def test_survives_a_restart_at_the_same_path(tmp_path):
    path = tmp_path / "seq.txt"
    first = SeqCounter(path)
    first.next()
    first.next()
    first.next()  # next call would be 3

    reopened = SeqCounter(path)
    assert reopened.next() == 3


def test_never_reuses_a_seq_across_many_restarts(tmp_path):
    path = tmp_path / "seq.txt"
    seen = []
    for _ in range(5):
        counter = SeqCounter(path)
        seen.append(counter.next())
    assert seen == [0, 1, 2, 3, 4]
