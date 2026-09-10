"""Bounded, cancellable URL downloads and offline document extraction."""
import copy
import hashlib
import math
import os
import re
import tempfile
import threading
import time
from dataclasses import dataclass
from email.message import Message
from gettext import gettext as _
from importlib.metadata import version
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import gi
gi.require_version('Soup', '3.0')
from gi.repository import Gio, GLib, Soup

from .knowledge import check_cancel, extract_document, make_document

MAX_DOWNLOAD = 50 * 1024 * 1024
MAX_CACHE = 200 * 1024 * 1024
MAX_URLS = 20
REQUEST_TIMEOUT = 60


def normalize_url(value):
    value = value.strip()
    try:
        if len(value) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise ValueError()
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname or parts.username is not None:
            raise ValueError()
        host = parts.hostname.encode('idna').decode('ascii').lower()
        if re.search(r'[\s%/\\]', host):
            raise ValueError()
        if ':' in host:
            host = '[' + host + ']'
        port = parts.port
        if port is not None and port != (443 if parts.scheme.lower() == 'https' else 80):
            host += ':' + str(port)
        return urlunsplit((parts.scheme.lower(), host, quote(parts.path or '/', safe="/%:@!$&'()*+,;=-._~"),
                           quote(parts.query, safe="/%?:@!$&'()*+,;=-._~"), ''))
    except (ValueError, UnicodeError) as exc:
        raise ValueError(_('Enter an HTTP or HTTPS URL without embedded login credentials.')) from exc


def parse_urls(text):
    urls = list(dict.fromkeys(normalize_url(line) for line in text.splitlines() if line.strip()))
    if not urls or len(urls) > MAX_URLS:
        raise ValueError(_('Enter between 1 and 20 URLs, one per line.'))
    return urls


@dataclass
class Download:
    source_url: str
    final_url: str
    content_type: str
    charset: str | None
    path: str
    size: int
    fetched_at: float
    file_hash: str

    def discard(self):
        Path(self.path).unlink(missing_ok=True)


def fetch_url(url, directory, cancel, progress=lambda text: None):
    source = current = normalize_url(url)
    request_cancel = Gio.Cancellable()
    handler = cancel.connect(lambda *args: request_cancel.cancel())
    timer = threading.Timer(REQUEST_TIMEOUT, request_cancel.cancel)
    timer.daemon = True
    session = Soup.Session(timeout=max(1, math.ceil(REQUEST_TIMEOUT)), user_agent='Gnollama URL Import')
    stream = None
    path = None
    timer.start()
    try:
        for redirect in range(6):
            check_cancel(cancel)
            progress(_('Downloading {0}').format(current))
            message = Soup.Message.new('GET', current)
            if message is None:
                raise ValueError(_('This URL could not be opened.'))
            message.set_flags(Soup.MessageFlags.NO_REDIRECT)
            stream = session.send(message, request_cancel)
            status = message.get_status()
            headers = message.get_response_headers()
            if status in (301, 302, 303, 307, 308):
                location = headers.get_one('Location')
                if not location or redirect == 5:
                    raise ValueError(_('The URL has an invalid redirect or exceeds five redirects.'))
                current = normalize_url(urljoin(current, location))
                stream.close(None)
                stream = None
                continue
            if not 200 <= status < 300:
                raise ValueError(_('Download failed: HTTP {0} {1}').format(status, message.get_reason_phrase()))
            if headers.get_content_length() > MAX_DOWNLOAD:
                raise ValueError(_('Downloads must be no larger than 50 MiB.'))
            mime = Message()
            mime['content-type'] = headers.get_one('Content-Type') or 'application/octet-stream'
            fd, path = tempfile.mkstemp(prefix='page-', dir=directory)
            size, digest = 0, hashlib.sha256()
            with os.fdopen(fd, 'wb') as output:
                while True:
                    chunk = stream.read_bytes(65536, request_cancel).get_data()
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise ValueError(_('Downloads must be no larger than 50 MiB.'))
                    digest.update(chunk)
                    output.write(chunk)
            check_cancel(cancel)
            result = Download(source, current, mime.get_content_type(), mime.get_content_charset(),
                              path, size, time.time(), digest.hexdigest())
            path = None
            return result
    except GLib.Error as exc:
        check_cancel(cancel)
        if request_cancel.is_cancelled():
            raise ValueError(_('The download timed out. Retry this URL.')) from exc
        raise ValueError(_('The URL could not be downloaded: {0}').format(exc.message)) from exc
    finally:
        timer.cancel()
        if handler:
            cancel.disconnect(handler)
        if stream is not None:
            try:
                stream.close(None)
            except GLib.Error:
                pass
        session.abort()
        if path:
            Path(path).unlink(missing_ok=True)


def _clean(tree):
    for node in list(tree.iter()):
        if not isinstance(node.tag, str):
            continue
        hidden = ('hidden' in node.attrib or node.get('aria-hidden', '').lower() == 'true'
                  or re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', node.get('style', ''), re.I))
        if node.tag.lower() in ('script', 'style', 'noscript', 'template', 'iframe', 'object', 'embed') or hidden:
            if node.getparent() is not None:
                node.drop_tree()
            else:
                node.clear()
        elif node.tag.lower() == 'a':
            href = node.get('href', '')
            try:
                valid = urlsplit(href).scheme.lower() in ('', 'http', 'https')
            except ValueError:
                valid = False
            if not valid:
                node.attrib.pop('href', None)


def _main_region(tree):
    regions = tree.xpath('//main | //*[@role="main"] | //article')
    if not regions:
        regions = tree.xpath('//div[@id="content" or @id="main-content" or @id="article-body" or '
                             'contains(concat(" ", normalize-space(@class), " "), " article-body ") or '
                             'contains(concat(" ", normalize-space(@class), " "), " entry-content ") or '
                             'contains(concat(" ", normalize-space(@class), " "), " markdown-body ") or '
                             'contains(concat(" ", normalize-space(@class), " "), " post-content ")]')
    if not regions:
        return None
    region = copy.deepcopy(max(regions, key=lambda node: len(node.text_content())))
    region.tail = None
    noise = re.compile(r'(?:^|[\s_-])(?:nav(?:igation)?|sidebar|cookie|advertisement|related|comments?)(?:$|[\s_-])', re.I)
    for node in list(region.iterdescendants()):
        if not isinstance(node.tag, str):
            continue
        if (node.tag in ('nav', 'aside', 'footer') or node.get('role') in ('navigation', 'complementary', 'contentinfo')
                or noise.search(node.get('id', '') + ' ' + node.get('class', ''))):
            if node.getparent() is not None:
                node.drop_tree()
    return html_string(region) if region.text_content().strip() else None


def html_string(tree):
    from lxml import html
    return html.tostring(tree, encoding='unicode')


def extract_html(raw, url, selector='', charset=None):
    from lxml import html
    from lxml.etree import ParserError
    from cssselect import SelectorError
    import trafilatura
    from markdownify import MarkdownConverter

    # Many modern pages omit a charset. Keep valid UTF-8 intact while allowing
    # lxml to honor an explicit HTML declaration or decode older encodings.
    if charset is None and not re.search(br'<meta\b[^>]*\bcharset\s*=', raw[:8192], re.I):
        try:
            raw.decode('utf-8')
        except UnicodeDecodeError:
            pass
        else:
            charset = 'utf-8'
    try:
        tree = html.fromstring(raw, parser=html.HTMLParser(encoding=charset, no_network=True, huge_tree=False))
    except (ParserError, LookupError, ValueError) as exc:
        raise ValueError(_('The page does not contain readable HTML.')) from exc
    title = ' '.join(tree.xpath('//title/text()')).strip()[:500]
    if not title:
        title = ' '.join(tree.xpath('//h1//text()')).strip()[:500]
    _clean(tree)
    tree.make_links_absolute(url, resolve_base_href=False)
    if selector.strip():
        try:
            matches = tree.cssselect(selector.strip())
        except (SelectorError, ValueError) as exc:
            raise ValueError(_('Invalid CSS selector: {0}').format(exc)) from exc
        selected = set(matches)
        roots = [node for node in matches if not any(parent in selected for parent in node.iterancestors())]
        if not roots:
            raise ValueError(_('The CSS selector did not match any elements. Try main, article, or a content class.'))
        body = html.Element('div')
        for node in roots:
            node = copy.deepcopy(node)
            node.tail = None
            body.append(node)
        content = html.tostring(body, encoding='unicode')
        extractor = 'css/markdownify-' + version('markdownify')
    else:
        content = _main_region(tree)
        extractor = 'main-region/markdownify-' + version('markdownify')
        if content is None:
            content = trafilatura.extract(tree, url=url, output_format='html', include_comments=False,
                                     include_tables=True, include_links=True, include_images=False,
                                     include_formatting=True)
            extractor = 'trafilatura-' + version('trafilatura')
    if not content:
        raise ValueError(_('No main content was found. Try a CSS selector, or paste the page text using Add Text.'))

    class Converter(MarkdownConverter):
        def convert_img(self, el, text, parent_tags):
            return el.get('alt', '')

    text = Converter(heading_style='ATX', strip_pre=None, wrap=False).convert(content).strip()
    if not text:
        raise ValueError(_('No text was found in the selected content. Try another CSS selector.'))
    return title or url, text, extractor


def extract_download(download, selector='', cancel=None):
    check_cancel(cancel)
    raw = Path(download.path).read_bytes()
    mime = download.content_type
    filename = unquote(urlsplit(download.final_url).path.rsplit('/', 1)[-1]) or _('Web document')
    is_html = mime in ('text/html', 'application/xhtml+xml')
    if mime == 'application/octet-stream' and re.match(br'\s*(?:<!doctype\s+html|<html\b)', raw, re.I):
        is_html = True
    if is_html:
        download.content_type = 'text/html'
        title, text, extractor = extract_html(raw, download.final_url, selector, download.charset)
        document, warnings = make_document(title, text, filename, file_hash=download.file_hash), []
    elif mime == 'application/pdf' or raw.startswith(b'%PDF-'):
        document, warnings = extract_document(filename if filename.lower().endswith('.pdf') else filename + '.pdf', raw, cancel)
        extractor = 'pypdf-' + version('pypdf')
    elif mime.startswith('text/') or mime in ('application/json', 'application/xml', 'application/octet-stream'):
        document, warnings = extract_document(filename, raw, cancel)
        extractor = 'utf8'
    else:
        raise ValueError(_('This URL must provide an HTML page, PDF, or UTF-8 text document.'))
    check_cancel(cancel)
    source = dict(source_url=download.source_url, final_url=download.final_url, fetched_at=download.fetched_at,
                  content_type='text/html' if is_html else mime, selector=selector.strip() if is_html else '',
                  extractor=extractor, edited=False)
    return document, source, warnings
