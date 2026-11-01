"""Destroy Qt fixtures while QApplication is alive, not during Python shutdown."""
import gc
import time
import unittest

from PySide6.QtCore import QCoreApplication, QEvent, QThread
from PySide6.QtWidgets import QApplication


class QtTestCase(unittest.TestCase):
    def tearDown(self):
        app = QApplication.instance()
        if app is not None:
            roots = [widget for widget in app.topLevelWidgets() if widget.parent() is None]
            for widget in roots:
                widget.close()
            deadline = time.monotonic() + 5
            while any(thread.isRunning() for widget in roots for thread in widget.findChildren(QThread)):
                if time.monotonic() >= deadline:
                    self.fail("Qt fixture still has a running worker after closing")
                app.processEvents()
                time.sleep(.005)
            for widget in roots:
                widget.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
            app.processEvents()
        gc.collect()
        super().tearDown()
