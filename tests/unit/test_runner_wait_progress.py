"""Repeated waiting retains an executable choice and describes its longer observation."""
from test_runner_control_bindings import candidate, manager, observation


def test_repeated_wait_stays_available_and_preserves_safe_alternatives():
    host, run = manager()
    run.recent_actions = [
        {"operation": "wait", "acknowledged": True, "page_changed": False},
        {"operation": "wait", "acknowledged": True, "page_changed": False},
    ]
    questions, heads, bindings = host._questions(run, observation({
        "button": candidate("button", "click", "Cancel report"),
    }))
    assert "wait" in questions[0].choices and "UNABLE" in questions[0].choices
    assert "click" in heads
    assert "up to one second" in bindings['wait']['short']['name']
