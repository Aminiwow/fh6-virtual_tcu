from virtual_tcu.config.store import ConfigStore
from virtual_tcu.input.keyboard_output import KeyboardOutput


def test_clutch_assist_wraps_shift_key(monkeypatch, tmp_path):
    config = ConfigStore(tmp_path / "config.json")
    config.set("feat_clutch_assist", True)
    config.set("clutch_key", "shift")
    config.set("clutch_pre_ms", 0)
    config.set("clutch_overlap_ms", 1)
    config.set("clutch_release_ms", 0)

    events: list[tuple[str, str]] = []

    monkeypatch.setattr("virtual_tcu.input.keyboard_output.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        "virtual_tcu.input.keyboard_output.keyboard.press",
        lambda key: events.append(("press", key)),
    )
    monkeypatch.setattr(
        "virtual_tcu.input.keyboard_output.keyboard.release",
        lambda key: events.append(("release", key)),
    )

    output = KeyboardOutput(config)
    try:
        output._press_release_with_clutch("e")
    finally:
        output.shutdown()

    assert events == [
        ("press", "shift"),
        ("press", "e"),
        ("release", "e"),
        ("release", "shift"),
    ]
