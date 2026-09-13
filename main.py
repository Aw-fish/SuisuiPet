"""Application entry point for SuisuiPet."""
import sys

from PySide6.QtWidgets import QApplication

from app import characters
from app.config import load_settings
from app.ui.icons import app_icon
from app.ui.pet_window import PetWindow
from app.ui.settings_window import SettingsWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(app_icon())
    # 保证当前角色的文件夹存在（幂等，只在启动时跑一次）
    _ = characters.ensure_character(load_settings()["character"]["selected"])
    settings = SettingsWindow()
    pet = PetWindow(settings.show_from_tray, settings.apply_external_settings)
    settings.settings_saved.connect(pet.apply_settings)
    # 重启即全新会话：历史由长期记忆承载；同时后台补做上次没整理完的会话
    pet.begin_new_session()
    pet.conversation.start_consolidation()
    pet.show_pet()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
