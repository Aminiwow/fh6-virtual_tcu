import sys
import types

sys.modules.setdefault(
    "keyboard",
    types.SimpleNamespace(
        add_hotkey=lambda *args, **kwargs: None,
        on_press_key=lambda *args, **kwargs: None,
        on_release_key=lambda *args, **kwargs: None,
        unhook_key=lambda *args, **kwargs: None,
        press=lambda *args, **kwargs: None,
        release=lambda *args, **kwargs: None,
    ),
)
