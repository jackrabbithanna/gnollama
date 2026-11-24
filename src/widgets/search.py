"""Search in string dropdowns."""
from gi.repository import Gtk


def searchable(dropdown):
    """Turn on a string dropdown's search. Without an expression to read, the search box filters nothing."""
    dropdown.set_expression(Gtk.PropertyExpression.new(Gtk.StringObject, None, 'string'))
    dropdown.set_search_match_mode(Gtk.StringFilterMatchMode.SUBSTRING)
    dropdown.set_enable_search(True)
    return dropdown
