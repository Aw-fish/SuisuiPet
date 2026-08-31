"""Application entry point for SuisuiPet."""
import sys
from PySide6.QtWidgets import QApplication
from app.ui.settings_window import SettingsWindow


def main() -> None:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    window = SettingsWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
