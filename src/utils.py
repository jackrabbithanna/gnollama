# utils.py
#
# Copyright 2025 Jackrabbithanna
#
# SPDX-License-Identifier: GPL-3.0-or-later

import html
import re

def parse_markdown(text):
    """
    Parses basic markdown into Pango markup for Gtk.Label.
    """
    # Escape HTML characters first
    text = html.escape(text)
    
    # Headers: ### Header -> <span size="large" weight="bold">Header</span>
    text = re.sub(r'^###\s+(.+)$', r'<span size="large" weight="bold">\1</span>', text, flags=re.MULTILINE)
    text = re.sub(r'^##\s+(.+)$', r'<span size="x-large" weight="bold">\1</span>', text, flags=re.MULTILINE)
    text = re.sub(r'^#\s+(.+)$', r'<span size="xx-large" weight="bold">\1</span>', text, flags=re.MULTILINE)
    
    # Bold: **text** -> <b>text</b>
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    
    # Italic: *text* -> <i>text</i>
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    
    # Inline code: `text` -> <tt>text</tt>
    text = re.sub(r'`(.+?)`', r'<tt>\1</tt>', text)
    
    # Math blocks: \[...\] -> <tt>...</tt> (basic fallback)
    text = re.sub(r'\\\[(.*?)\\\]', r'<tt>\1</tt>', text, flags=re.DOTALL)
    
    # Inline math: \(...\) -> <tt>...</tt>
    text = re.sub(r'\\\((.*?)\\\)', r'<tt>\1</tt>', text)
    
    return text
