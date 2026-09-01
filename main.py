"""Application entry point for SuisuiPet."""
import sys

from PySide6.QtWidgets import QApplication

from app.ui.pet_window import PetWindow
from app.ui.settings_window import SettingsWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    settings = SettingsWindow()
    pet = PetWindow(settings.show_from_tray)
    pet.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
