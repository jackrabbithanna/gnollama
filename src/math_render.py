"""TeX math: display equations become textures, inline math becomes Pango markup."""
import html
import math
import re
from functools import lru_cache
from typing import Any, List, Optional, Tuple

from gi.repository import Gdk, GLib

DISPLAY_MATH = re.compile(
    r'\\\[(.+?)\\\]|\$\$(.+?)\$\$'
    r'|^[ \t]*(\\begin\{([A-Za-z]+\*?)\}.+?\\end\{\4\})[ \t]*$',  # An environment with no delimiters around it
    re.S | re.M)
# Signs that a line with no delimiters at all is an equation.
BARE_COMMAND = re.compile(r'\\(?:d?frac|int|sum|prod|sqrt|lim|partial|nabla)(?![A-Za-z])')
NOT_BARE = re.compile(r'[`$|&]|\\[()\[\]]|\\(?:begin|end)(?![A-Za-z])|^\s*(?:[-*+>#]|\d+[.)])\s')
WORDS_IN_TEX = re.compile(r'\\(?:text|textrm|textbf|mathrm|mathbf|operatorname)\s*\{[^{}]*\}|\\[A-Za-z]+')
ROW_SPACING = re.compile(r'\\\\\s*\[[^\]\n]*\]')  # \\[4pt]
INLINE_MATH = re.compile(
    r'(?P<code>(`+).+?\2)'  # Code spans are left alone.
    r'|\\\((?P<paren>(?:[^\n]|\n(?!\s*\n))+?)\\\)'
    r'|(?<![\\$\w=/&?#])\$(?P<dollar>[^\s$](?:[^$\n]*?[^\s$\\])?)\$(?![\w$])'
    r'|(?P<open>\\\[.*\Z)',  # A display equation that is still streaming in.
    re.S)
PLACEHOLDER = re.compile('(\\d+)')
INLINE_SPAN = re.compile('(.*?)', re.S)


def split_display(text: str) -> List[Tuple[str, str]]:
    """Splits text into ('text', markdown) and ('math', tex) pieces."""
    pieces: List[Tuple[str, str]] = []
    pos = 0
    for match in DISPLAY_MATH.finditer(text):
        line_start = text.rfind('\n', 0, match.start()) + 1
        if text.count('`', line_start, match.start()) % 2:
            continue  # Inside a code span
        tex = next(group for group in match.group(1, 2, 3) if group is not None).strip()
        if not tex:
            continue
        pieces += _split_bare(text[pos:match.start()]) + [('math', tex)]
        pos = match.end()
    pieces += _split_bare(text[pos:])
    if len(pieces) == 1:
        return [('text', text)]
    return [piece for piece in pieces if piece[0] == 'math' or piece[1].strip()]


def _is_bare_equation(line: str) -> bool:
    """True for a line that is only TeX, such as a reply that is just an integral."""
    if not BARE_COMMAND.search(line) or NOT_BARE.search(line) or not re.search(r'[=^_{]', line):
        return False
    return not re.search(r'[A-Za-z]{4,}', WORDS_IN_TEX.sub(' ', line))


def _split_bare(text: str) -> List[Tuple[str, str]]:
    """Splits out the lines that are equations written without delimiters."""
    pieces: List[Tuple[str, str]] = []
    lines: List[str] = []
    for line in text.split('\n'):
        if _is_bare_equation(line):
            pieces += [('text', '\n'.join(lines)), ('math', line.strip())]
            lines = []
        else:
            lines.append(line)
    if not pieces:
        return [('text', text)]
    return pieces + [('text', '\n'.join(lines))]


def protect(text: str) -> Tuple[str, List[str]]:
    """Swaps inline math for placeholders that Markdown passes through untouched.
    Returns the new text and what to put back for each placeholder."""
    spans: List[str] = []

    def replace(match: Any) -> str:
        if match['code']:
            return match[0]
        if match['open']:
            markup = '<tt>' + html.escape(match[0]) + '</tt>'
        else:
            tex = match['paren'] if match['paren'] is not None else match['dollar']
            # "$5 and $10" is money, "$5x^2$" is math.
            if match['dollar'] and tex[0].isdigit() and not re.search(r'[\\^_{}=]', tex):
                return match[0]
            markup = '' + html.escape(tex) + ''
        spans.append(markup)
        return '%d' % (len(spans) - 1)

    return INLINE_MATH.sub(replace, text), spans


def restore(markup: str, spans: List[str]) -> str:
    """Puts inline math back in place of its placeholders, as TeX marked for expand()."""
    return PLACEHOLDER.sub(lambda match: spans[int(match[1])], markup) if spans else markup


def expand(markup: str, convert: Any) -> str:
    """Replaces each marked inline equation with the markup convert(tex) returns."""
    return INLINE_SPAN.sub(lambda match: convert(html.unescape(match[1])), markup)


def to_text(markup: str) -> str:
    """Pango markup with every inline equation as text."""
    return expand(markup, inline_markup)


@lru_cache(maxsize=1024)
def render(tex: str, font_px: int, scale: int,
           inline: bool = False) -> Optional[Tuple[Gdk.Texture, float, float, float]]:
    """Renders an equation in white on a transparent background, for use as a mask.
    Returns the texture, its width and height and its height above the baseline,
    in logical pixels, or None if it cannot be rendered."""
    if not tex.strip():
        return None
    try:
        import ziamath
        tex = ROW_SPACING.sub(lambda match: r'\\', tex)
        svg = ziamath.Latex(tex, size=font_px * scale, color='#ffffff', inline=inline).svg()
        # The baseline is at y = 0, so the top edge of the view box is the ascent.
        view_box = re.search(r'viewBox="\S+ (\S+) ', svg)
        if view_box is None:
            raise ValueError('The rendered equation has no view box')
        top = float(view_box[1])
        texture = _rasterize(svg)
    except Exception:
        return None
    return texture, texture.get_width() / scale, texture.get_height() / scale, -top / scale


def _rasterize(svg: str) -> Gdk.Texture:
    """Draws the SVG in this process. Gdk.Texture would start a sandboxed
    loader for every equation, which is slow and fails inside some sandboxes."""
    try:
        import cairo
        import gi
        gi.require_version('Rsvg', '2.0')
        from gi.repository import Rsvg
    except (ImportError, ValueError):
        return Gdk.Texture.new_from_bytes(GLib.Bytes.new(svg.encode()))
    handle = Rsvg.Handle.new_from_data(svg.encode())
    viewport = Rsvg.Rectangle()
    known, viewport.width, viewport.height = handle.get_intrinsic_size_in_pixels()
    if not known:
        raise ValueError('SVG without a size')
    width, height = math.ceil(viewport.width), math.ceil(viewport.height)
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    handle.render_document(cairo.Context(surface), viewport)
    surface.flush()
    return Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.B8G8R8A8_PREMULTIPLIED,
                                 GLib.Bytes.new(bytes(surface.get_data())), surface.get_stride())


SYMBOLS = {
    'alpha': 'α', 'beta': 'β', 'gamma': 'γ', 'delta': 'δ', 'epsilon': 'ε', 'varepsilon': 'ε', 'zeta': 'ζ',
    'eta': 'η', 'theta': 'θ', 'vartheta': 'ϑ', 'iota': 'ι', 'kappa': 'κ', 'lambda': 'λ', 'mu': 'μ', 'nu': 'ν',
    'xi': 'ξ', 'pi': 'π', 'rho': 'ρ', 'sigma': 'σ', 'tau': 'τ', 'upsilon': 'υ', 'phi': 'φ', 'varphi': 'φ',
    'chi': 'χ', 'psi': 'ψ', 'omega': 'ω', 'Gamma': 'Γ', 'Delta': 'Δ', 'Theta': 'Θ', 'Lambda': 'Λ', 'Xi': 'Ξ',
    'Pi': 'Π', 'Sigma': 'Σ', 'Phi': 'Φ', 'Psi': 'Ψ', 'Omega': 'Ω',
    'leq': '≤', 'le': '≤', 'geq': '≥', 'ge': '≥', 'neq': '≠', 'ne': '≠', 'approx': '≈', 'equiv': '≡',
    'sim': '∼', 'propto': '∝', 'll': '≪', 'gg': '≫', 'times': '×', 'cdot': '·', 'div': '÷', 'pm': '±',
    'mp': '∓', 'ast': '∗', 'circ': '∘', 'to': '→', 'rightarrow': '→', 'leftarrow': '←', 'Rightarrow': '⇒',
    'Leftarrow': '⇐', 'leftrightarrow': '↔', 'Leftrightarrow': '⇔', 'implies': '⟹', 'iff': '⟺', 'mapsto': '↦',
    'infty': '∞', 'partial': '∂', 'nabla': '∇', 'sum': '∑', 'prod': '∏', 'int': '∫', 'oint': '∮',
    'in': '∈', 'notin': '∉', 'subset': '⊂', 'subseteq': '⊆', 'supset': '⊃', 'supseteq': '⊇', 'cup': '∪',
    'cap': '∩', 'emptyset': '∅', 'varnothing': '∅', 'forall': '∀', 'exists': '∃', 'neg': '¬', 'land': '∧',
    'lor': '∨', 'wedge': '∧', 'vee': '∨', 'oplus': '⊕', 'otimes': '⊗', 'perp': '⊥', 'parallel': '∥',
    'angle': '∠', 'degree': '°', 'prime': '′', 'ell': 'ℓ', 'hbar': 'ℏ', 'Re': 'ℜ', 'Im': 'ℑ', 'aleph': 'ℵ',
    'ldots': '…', 'dots': '…', 'cdots': '⋯', 'langle': '⟨', 'rangle': '⟩', 'lfloor': '⌊', 'rfloor': '⌋',
    'lceil': '⌈', 'rceil': '⌉', 'lvert': '|', 'rvert': '|', 'mid': '|', 'vert': '|', 'lVert': '‖', 'rVert': '‖',
    'quad': '  ', 'qquad': '    ', 'left': '', 'right': '', 'big': '', 'Big': '', 'bigg': '', 'Bigg': '',
    'displaystyle': '', 'textstyle': '', 'limits': '', 'nolimits': '',
}
FUNCTIONS = set('sin cos tan cot sec csc arcsin arccos arctan sinh cosh tanh log ln lg exp lim sup inf max min '
                'det dim ker deg gcd arg mod Pr'.split())
ESCAPES = {',': ' ', ';': ' ', ':': ' ', ' ': ' ', '!': '', '|': '‖'}
BLACKBOARD = {'N': 'ℕ', 'Z': 'ℤ', 'Q': 'ℚ', 'R': 'ℝ', 'C': 'ℂ', 'P': 'ℙ', 'E': '𝔼', 'F': '𝔽', 'H': 'ℍ'}
UPRIGHT = ('text', 'textrm', 'mathrm', 'operatorname', 'textit', 'mathit', 'textnormal')
BOLD = ('mathbf', 'textbf', 'boldsymbol', 'bm')
TOKEN = re.compile(r'\\([A-Za-z]+)|\\(.)|([A-Za-z]+)|(\s+)|(.)', re.S)


class Unsupported(Exception):
    """The TeX needs real layout, so it is shown as source instead."""


def inline_markup(tex: str) -> str:
    """Pango markup for inline math: simple TeX as Unicode, anything else as its source."""
    try:
        markup, pos = _sequence(TOKEN.findall(tex), 0, True)
        return markup.strip()
    except (Unsupported, IndexError):
        return '<tt>' + html.escape(tex) + '</tt>'


def _plain(markup: str) -> str:
    return html.unescape(re.sub(r'<[^>]+>', '', markup))


def _wrapped(markup: str) -> str:
    """Adds parentheses unless the markup is a single word or number."""
    return markup if re.fullmatch(r'[\w.]+', _plain(markup)) else '(' + markup + ')'


def _sequence(tokens: List[Tuple[str, ...]], pos: int, italic: bool, group: bool = False) -> Tuple[str, int]:
    """Converts tokens up to the end, or up to the closing brace of a group."""
    parts: List[str] = []
    while pos < len(tokens):
        if tokens[pos][4] == '}':
            if not group:
                raise Unsupported
            return ''.join(parts), pos + 1
        markup, pos = _atom(tokens, pos, italic)
        parts.append(markup)
    if group:
        raise Unsupported
    return ''.join(parts), pos


def _argument(tokens: List[Tuple[str, ...]], pos: int, italic: bool) -> Tuple[str, int]:
    """Converts the next token or braced group, skipping the spaces before it."""
    while tokens[pos][3]:
        pos += 1
    return _atom(tokens, pos, italic)


def _atom(tokens: List[Tuple[str, ...]], pos: int, italic: bool) -> Tuple[str, int]:
    """Converts one token, or one braced group, with whatever arguments it takes."""
    command, escape, letters, space, char = tokens[pos]
    pos += 1
    if letters:
        return ('<i>%s</i>' % letters if italic else letters), pos
    if space:
        return ' ', pos
    if escape:
        if escape in '%$_{}&#':
            return html.escape(escape), pos
        if escape in ESCAPES:
            return ESCAPES[escape], pos
        raise Unsupported
    if command:
        if command in ('left', 'right') and pos < len(tokens) and tokens[pos][4] == '.':
            return '', pos + 1
        if command in SYMBOLS:
            return SYMBOLS[command], pos
        if command in FUNCTIONS:
            return command, pos
        if command in ('frac', 'dfrac', 'tfrac'):
            above, pos = _argument(tokens, pos, italic)
            below, pos = _argument(tokens, pos, italic)
            return _wrapped(above) + '/' + _wrapped(below), pos
        if command == 'sqrt':
            inner, pos = _argument(tokens, pos, italic)
            if inner == '[':
                raise Unsupported  # \sqrt[n]{x}
            return '√' + _wrapped(inner), pos
        if command in UPRIGHT:
            return _argument(tokens, pos, False)
        if command in BOLD:
            inner, pos = _argument(tokens, pos, False)
            return '<b>%s</b>' % inner, pos
        if command == 'mathbb':
            inner, pos = _argument(tokens, pos, False)
            if inner not in BLACKBOARD:
                raise Unsupported
            return BLACKBOARD[inner], pos
        raise Unsupported
    if char == '{':
        return _sequence(tokens, pos, italic, True)
    if char in '^_':
        inner, pos = _argument(tokens, pos, italic)
        return ('<sup>%s</sup>' if char == '^' else '<sub>%s</sub>') % inner, pos
    if char in '&}':
        raise Unsupported
    return {'-': '−', "'": '′', '~': ' '}.get(char, html.escape(char)), pos
