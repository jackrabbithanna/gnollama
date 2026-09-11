"""Shared clipboard and nonblocking feedback actions."""
from gettext import gettext as _
from gi.repository import Adw


def toast(widget, message):
    root = widget.get_root()
    overlay = getattr(root, 'toast_overlay', None)
    if overlay is not None:
        overlay.add_toast(Adw.Toast(title=message))


def copy_text(widget, text):
    widget.get_clipboard().set(text)
    toast(widget, _('Copied'))
