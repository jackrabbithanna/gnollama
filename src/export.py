"""Portable, credential-free conversation snapshots."""
import copy
import json


def portable(value):
    if isinstance(value, dict):
        return {k: portable(v) for k, v in value.items()
                if k.lower() not in ('credential_id', 'credential_ref', 'credential', 'credentials',
                                     'api_key', 'api-key', 'authorization', 'access_token', 'password')}
    if isinstance(value, list):
        return [portable(v) for v in value]
    return value


def export_json(snapshot):
    return json.dumps({'format': 'gnollama-conversation', 'version': 1,
                       'conversation': portable(copy.deepcopy(snapshot))}, ensure_ascii=False, indent=2) + '\n'


def export_markdown(snapshot):
    snapshot = portable(snapshot)
    parts = ['# ' + snapshot['title'], '']
    if snapshot.get('system'):
        parts += ['## System', '', snapshot['system'], '']
    for message in snapshot.get('messages', []):
        parts += ['## ' + message.get('model', message['role'].title()), '', message.get('content', ''), '']
        for index, encoded in enumerate(message.get('images', [])):
            import base64
            raw = base64.b64decode(encoded.split(',', 1)[-1])
            mime = 'image/png' if raw.startswith(b'\x89PNG') else 'image/webp' if raw[8:12] == b'WEBP' else 'image/jpeg'
            parts += [f'![Attachment {index + 1}](data:{mime};base64,{encoded})', '']
        metadata = message.get('response_metadata') or {}
        if metadata.get('status'):
            parts += ['Status: ' + metadata['status'], '']
        for hit in (metadata.get('retrieval') or {}).get('hits', []):
            parts += ['### ' + str(hit.get('label', hit.get('id', 'Source'))), '',
                      str(hit.get('title', '')), '', hit.get('text', ''), '']
    return '\n'.join(parts)


def choose_export(parent, storage, chat_id, format):
    from gi.repository import Gtk, Gio, GLib
    from gettext import gettext as _
    from .widgets.feedback import toast
    dialog = Gtk.FileDialog(title=_('Export Conversation'), initial_name='conversation.' + ('json' if format == 'json' else 'md'))
    def chosen(dialog, result):
        try:
            file = dialog.save_finish(result)
        except GLib.Error:
            return
        # The writer barrier and read transaction define a saved snapshot.
        def ready():
            def write():
                try:
                    snapshot = storage.export_snapshot(chat_id)
                    if snapshot is None:
                        raise ValueError(_('Conversation no longer exists.'))
                    text = export_json(snapshot) if format == 'json' else export_markdown(snapshot)
                    file.replace_contents(text.encode('utf-8'), None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
                    GLib.idle_add(toast, parent, _('Conversation exported'))
                except Exception as exc:
                    GLib.idle_add(toast, parent, str(exc))
            storage.services.control.submit(write)
        storage._submit(lambda: None, on_done=ready)
    dialog.save(parent, None, chosen)
