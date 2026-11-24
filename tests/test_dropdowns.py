"""Searchable dropdowns filter by what is typed."""
import unittest

from gi.repository import Gtk
from src.widgets.search import searchable


class SearchableTests(unittest.TestCase):
    def test_search_reads_each_string_and_matches_anywhere_in_it(self):
        dropdown = searchable(Gtk.DropDown.new_from_strings(['google/gemma', 'k2-horizon']))
        self.assertTrue(dropdown.get_enable_search())
        self.assertEqual(dropdown.get_search_match_mode(), Gtk.StringFilterMatchMode.SUBSTRING)
        # The popover filters with this expression; without one it shows every item whatever is typed.
        strings = Gtk.StringFilter(expression=dropdown.get_expression(), search='HORIZON',
                                   match_mode=dropdown.get_search_match_mode())
        shown = Gtk.FilterListModel(model=dropdown.get_model(), filter=strings)
        self.assertEqual([shown.get_item(i).get_string() for i in range(shown.get_n_items())], ['k2-horizon'])


if __name__ == '__main__':
    unittest.main()
