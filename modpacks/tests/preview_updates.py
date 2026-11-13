"""Show the update popup with sample data; no downloads or game files are used."""
import tkinter as tk

from test_launcher_updates import dialog_style, sample_update_packs, window


if __name__ == "__main__":
    root = tk.Tk()
    root.withdraw()
    with dialog_style():
        dialog = window.UpdateDialog(root, sample_update_packs())
        dialog.title("Modpack updates — sample preview")
        dialog.wait_window()
    root.destroy()
