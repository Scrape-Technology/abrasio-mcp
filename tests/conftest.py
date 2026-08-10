"""
Stub the real `abrasio` browser SDK so unit tests for modules that don't actually
depend on it (e.g. `abrasio_mcp.recording`, which is pure stdlib Python) don't require
the heavy Abrasio package to be installed in the test environment.

`abrasio_mcp/__init__.py` eagerly does `from .browser import AbrasioBrowserAgent`, and
`browser.py` does `from abrasio import Abrasio, AbrasioConfig` plus
`from abrasio.human.actions import ...` — so importing anything under the `abrasio_mcp`
package (even `abrasio_mcp.recording`) transitively needs those names to exist.
"""

import sys
import types

if "abrasio" not in sys.modules:
    fake_abrasio = types.ModuleType("abrasio")
    fake_abrasio.Abrasio = type("Abrasio", (), {})
    fake_abrasio.AbrasioConfig = type("AbrasioConfig", (), {})
    sys.modules["abrasio"] = fake_abrasio

    fake_human = types.ModuleType("abrasio.human")
    sys.modules["abrasio.human"] = fake_human

    fake_actions = types.ModuleType("abrasio.human.actions")
    for _name in (
        "click_human", "fill_human", "type_human", "scroll_human",
        "human_mouse_move", "_resolve_box_async",
    ):
        setattr(fake_actions, _name, lambda *a, **k: None)
    sys.modules["abrasio.human.actions"] = fake_actions
