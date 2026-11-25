"""Image files decoded with GTK's loaders, for the headless modules that store them."""
from gi.repository import Gdk, GLib

from .widgets.decision_images import reduced


def reduced_png(raw, side):
    """The image in `raw` as PNG bytes no longer than `side` on its longer side, with its width and height.

    Safe on a worker. Raises GLib.Error for bytes no loader reads.
    """
    texture = reduced(Gdk.Texture.new_from_bytes(GLib.Bytes.new(raw)), side)
    return bytes(texture.save_to_png_bytes().get_data()), texture.get_width(), texture.get_height()
