from transcription_bot.work_queue import Job, WorkQueue


def test_jobs_survive_restart(tmp_path):
    path = tmp_path / "sub" / "queue.sqlite3"
    q = WorkQueue(path)
    assert q.add(1, 10)
    assert q.add(2, 20)
    assert not q.add(1, 10)  # duplicate
    assert q.start_attempt(1, 10) == 1
    assert q.start_attempt(1, 10) == 2
    q.close()  # simulated crash/restart

    q = WorkQueue(path)
    assert q.pending() == [Job(1, 10, 2), Job(2, 20, 0)]
    assert q.start_attempt(1, 10) == 3
    q.remove(1, 10)
    q.close()

    q = WorkQueue(path)
    assert q.pending() == [Job(2, 20, 0)]
    q.close()


def test_start_attempt_creates_missing_job(queue):
    assert queue.start_attempt(5, 50) == 1
    assert queue.pending() == [Job(5, 50, 1)]
