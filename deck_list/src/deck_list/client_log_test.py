"""What the page notices (failed or retried saves, page errors) ends up in the app's log."""

from loguru import logger

from deck_list import runner


def test_page_reports_are_written_to_the_log() -> None:
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level="WARNING")
    try:
        answer = runner.app.test_client().post("/api/client-log", json={"entries": [
            {"time": "2026-11-12T10:00:00Z", "page": "/?deck=ulalek", "message": "server call failed after retrying",
             "detail": '{"path": "/api/decks/ulalek/tag", "problem": "HTTP 502, not JSON: <html>"}'},
        ]})
    finally:
        logger.remove(sink)
    assert answer.status_code == 200 and answer.get_json() == {"logged": 1}
    assert len(lines) == 1
    assert "Page reported: server call failed after retrying" in lines[0] and "/api/decks/ulalek/tag" in lines[0]


def test_at_most_fifty_reports_are_logged_per_call() -> None:
    entries = [{"time": "t", "page": "/", "message": f"m{i}"} for i in range(80)]
    answer = runner.app.test_client().post("/api/client-log", json={"entries": entries})
    assert answer.get_json() == {"logged": 50}
