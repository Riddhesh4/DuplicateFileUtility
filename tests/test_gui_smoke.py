import threading
import time

import pytest

from duplicate_finder import gui


def test_gui_starts_and_stops():
    # instantiate the GUI briefly and destroy without running full mainloop
    app = gui.DuplicateFinderApp()
    # perform a single update cycle and then destroy
    app.update_idletasks()
    app.destroy()
