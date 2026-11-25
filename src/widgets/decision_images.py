"""The images of a decision: read once and kept small, shown at a bounded size, and made PNG for a host that asks."""
import base64
import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from gi.repository import Gdk, GLib, GObject, Gtk
from ..providers.systemone import bare, image_kind

# The longest side kept of an image that is shown, in pixels; twice what the largest view of one asks for.
KEPT_SIDE = 960
THUMBNAIL = 80
# The images read last, by a digest of their data, and who waits for one being read.
RECENT = 16
_seen: OrderedDict = OrderedDict()
_waiting: dict = {}


@dataclass(frozen=True)
class Seen:
    """An image as it is shown, and the size it was sent at."""
    texture: Gdk.Texture
    width: int
    height: int


class Bounded(GObject.Object, Gdk.Paintable):
    """A texture that asks for at most `side` pixels either way and keeps its shape."""
    def __init__(self, texture, side):
        super().__init__()
        self.texture = texture
        scale = min(1, side / max(texture.get_width(), texture.get_height()))
        self.width = max(1, round(texture.get_width() * scale))
        self.height = max(1, round(texture.get_height() * scale))

    def do_get_intrinsic_width(self):
        return self.width

    def do_get_intrinsic_height(self):
        return self.height

    def do_get_intrinsic_aspect_ratio(self):
        return self.texture.get_intrinsic_aspect_ratio()

    def do_get_flags(self):
        return Gdk.PaintableFlags.SIZE | Gdk.PaintableFlags.CONTENTS

    def do_snapshot(self, snapshot, width, height):
        self.texture.snapshot(snapshot, width, height)


def texture_of(image):
    """The texture of an image given as base64; raises ValueError or GLib.Error for one that cannot be read."""
    return Gdk.Texture.new_from_bytes(GLib.Bytes.new(base64.b64decode(bare(image))))


def reduced(texture, side=KEPT_SIDE):
    """The texture, or a copy no longer than `side` on its longer side."""
    width, height = texture.get_width(), texture.get_height()
    scale = side / max(width, height)
    if scale >= 1:
        return texture
    try:
        import cairo
    except ImportError:
        return texture
    downloader = Gdk.TextureDownloader.new(texture)
    downloader.set_format(Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED)
    data, stride = downloader.download_bytes()
    source = cairo.ImageSurface.create_for_data(bytearray(data.get_data()), cairo.FORMAT_ARGB32, width, height, stride)
    small = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, round(width * scale)), max(1, round(height * scale)))
    context = cairo.Context(small)
    context.scale(small.get_width() / width, small.get_height() / height)
    context.set_source_surface(source)
    context.get_source().set_filter(cairo.FILTER_GOOD)
    context.paint()
    small.flush()
    return Gdk.MemoryTexture.new(small.get_width(), small.get_height(), Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(bytes(small.get_data())), small.get_stride())


def load(image, done, submit=None):
    """Hand `done` the Seen of a base64 image, or None for one that cannot be read.

    Call on the main context. With `submit` the image is read on a worker and
    `done` is called later; an image read lately is handed over at once.
    """
    key = hashlib.blake2b(image.encode(), digest_size=16).digest()
    if key in _seen:
        _seen.move_to_end(key)
        done(_seen[key])
        return
    if key in _waiting:
        _waiting[key].append(done)
        return
    _waiting[key] = [done]

    def read():
        try:
            texture = texture_of(image)
            return Seen(reduced(texture), texture.get_width(), texture.get_height())
        except (ValueError, GLib.Error):
            return None

    def ready(seen):
        _seen[key] = seen
        while len(_seen) > RECENT:
            _seen.popitem(last=False)
        for waiting in _waiting.pop(key):
            waiting(seen)
        return False
    if submit:
        submit(lambda: GLib.idle_add(ready, read()))
    else:
        ready(read())


def thumbnail(image, label, submit=None):
    """A small picture of a base64 image, empty until it is read and for good if it cannot be."""
    picture = Gtk.Picture(can_shrink=False, halign=Gtk.Align.START, valign=Gtk.Align.START, alternative_text=label,
                          visible=False)

    def show(seen):
        if seen:
            picture.set_paintable(Bounded(seen.texture, THUMBNAIL))
            picture.set_visible(True)
    load(image, show, submit)
    return picture


def to_png(image):
    """A base64 image as PNG; one that cannot be read is returned as it is, for the server to refuse."""
    try:
        return base64.b64encode(texture_of(image).save_to_png_bytes().get_data()).decode('ascii')
    except (ValueError, GLib.Error):
        return image


def in_formats(images, formats):
    """The images, each made PNG unless it is already in one of the named `formats`. Safe on a worker."""
    return [image if image_kind(image) in formats else to_png(image) for image in images]
