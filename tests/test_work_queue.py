import sqlite3

from transcription_bot.work_queue import WorkQueue


def keys(queue):
    return [(job.chat_id, job.message_id, job.attempts) for job in queue.pending()]


def test_jobs_survive_restart(tmp_path):
    path = tmp_path / "sub" / "queue.sqlite3"
    q = WorkQueue(path)
    q.add(1, 10)
    q.add(2, 20)
    q.add(1, 10)  # duplicate
    assert q.start_attempt(1, 10).attempts == 1
    assert q.start_attempt(1, 10).attempts == 2
    q.close()  # simulated crash/restart

    q = WorkQueue(path)
    assert keys(q) == [(1, 10, 2), (2, 20, 0)]
    assert q.start_attempt(1, 10).attempts == 3
    q.remove(1, 10)
    q.close()

    q = WorkQueue(path)
    assert keys(q) == [(2, 20, 0)]
    q.close()


def test_start_attempt_creates_missing_job(queue):
    job = queue.start_attempt(5, 50)
    assert (job.attempts, job.text, job.sent_chunks) == (1, None, 0)
    assert keys(queue) == [(5, 50, 1)]


def test_end_attempt_takes_the_attempt_back(queue):
    queue.start_attempt(1, 10)
    queue.end_attempt(1, 10)
    queue.end_attempt(1, 10)
    assert keys(queue) == [(1, 10, 0)]


def test_transcript_and_sent_chunks_are_kept(tmp_path):
    path = tmp_path / "queue.sqlite3"
    q = WorkQueue(path)
    q.add(1, 10)
    added_at = q.pending()[0].added_at
    q.save_text(1, 10, "long text")
    q.save_sent_chunks(1, 10, 2)
    q.close()
    q = WorkQueue(path)
    job = q.start_attempt(1, 10)
    assert (job.text, job.sent_chunks, job.added_at) == ("long text", 2, added_at)
    q.close()


def test_heartbeat(tmp_path):
    q = WorkQueue(tmp_path / "queue.sqlite3")
    assert q.heartbeat() is None
    q.set_heartbeat(1000.5)
    q.set_heartbeat(2000.5)
    q.close()
    assert WorkQueue(tmp_path / "queue.sqlite3").heartbeat() == 2000.5


def test_queue_file_of_the_previous_version_is_upgraded(tmp_path):
    path = tmp_path / "queue.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE jobs (chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL,"
               " attempts INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL, PRIMARY KEY (chat_id, message_id))")
    db.execute("INSERT INTO jobs VALUES (1, 10, 2, 123.0)")
    db.commit()
    db.close()
    q = WorkQueue(path)
    job = q.start_attempt(1, 10)
    assert (job.attempts, job.added_at, job.text, job.sent_chunks) == (3, 123.0, None, 0)
    q.close()
