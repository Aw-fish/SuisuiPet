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
    pet.show_pet()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
