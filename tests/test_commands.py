from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMenu, QStyleOptionMenuItem

from choicer_voicer_pack_creator.ui.commands import action_button, describe_action
from choicer_voicer_pack_creator.ui.theme import APP_STYLESHEET


@pytest.mark.parametrize("stylesheet", ["", APP_STYLESHEET], ids=["native", "themed"])
@pytest.mark.parametrize("checkable", [False, True], ids=["command", "preference"])
def test_menu_preferences_use_native_checks_without_losing_button_icons(
    qtbot, stylesheet, checkable,
):
    menu = QMenu()
    qtbot.addWidget(menu)
    menu.setStyleSheet(stylesheet)
    action = menu.addAction("Update preference" if checkable else "Check for Updates...")
    action.setCheckable(checkable)
    describe_action(action, "restore", "Application updates.")
    button = action_button(action, menu)

    assert action.isIconVisibleInMenu() is not checkable
    assert not action.icon().isNull()
    assert not button.icon().isNull()
    assert button.defaultAction() is action
    assert action.toolTip() == action.statusTip() == "Application updates."
    for checked in ([False, True, False] if checkable else [False]):
        action.setChecked(checked)
        option = QStyleOptionMenuItem()
        menu.initStyleOption(option, action)

        assert option.checkType == (
            QStyleOptionMenuItem.CheckType.NonExclusive if checkable
            else QStyleOptionMenuItem.CheckType.NotCheckable
        )
        assert option.checked is checked
        assert option.icon.isNull() is checkable
        assert button.isCheckable() is checkable
        assert button.isChecked() is checked
        assert not button.icon().isNull()
