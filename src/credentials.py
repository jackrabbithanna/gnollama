"""Per-host secrets, isolated from host records and conversation persistence."""
from gettext import gettext as _
from threading import RLock

import gi

gi.require_version('Secret', '1')
from gi.repository import GLib, Secret


class CredentialError(Exception):
    """A recoverable credential or keyring error; never includes secret values."""


class CredentialStore:
    def __init__(self):
        self._session = {}
        self._lock = RLock()
        self.schema = Secret.Schema.new(
            'io.github.jackrabbithanna.Gnollama.ApiKey', Secret.SchemaFlags.NONE,
            {'credential': Secret.SchemaAttributeType.STRING})

    @staticmethod
    def validate(key):
        key = key.strip()
        if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
            raise CredentialError(_('Enter a valid API key.'))
        return key

    def has_session(self, host_id):
        with self._lock:
            return host_id in self._session

    def set_session(self, host_id, key):
        with self._lock:
            self._session[host_id] = self.validate(key)

    def forget_session(self, host_id):
        with self._lock:
            self._session.pop(host_id, None)

    def lookup(self, host_id, reference, cancellable=None):
        with self._lock:
            key = self._session.get(host_id)
        if key:
            return key
        if reference:
            try:
                key = Secret.password_lookup_sync(self.schema, {'credential': reference}, cancellable)
            except GLib.Error:
                raise CredentialError(_('Could not access the keyring. Edit this host to retry or enter a session-only key.')) from None
        if not key:
            raise CredentialError(_('An API key is required. Edit this host to enter one.'))
        return self.validate(key)

    def save(self, reference, key, cancellable=None):
        key = self.validate(key)
        try:
            saved = Secret.password_store_sync(self.schema, {'credential': reference},
                                               Secret.COLLECTION_DEFAULT, 'Gnollama — Ollama Cloud',
                                               key, cancellable)
            if saved:
                return
        except GLib.Error:
            pass
        raise CredentialError(_('Could not save the API key in the keyring. Retry or use it for this session.'))

    def clear(self, reference, cancellable=None):
        if reference:
            try:
                attributes = {'credential': reference}
                Secret.password_clear_sync(self.schema, attributes, cancellable)
                # clear() only removes unlocked items. Search without retrieving
                # secrets so a locked item cannot be orphaned by deleting its host.
                remaining = Secret.password_search_sync(self.schema, attributes,
                                                        Secret.SearchFlags.ALL, cancellable)
                if remaining:
                    raise CredentialError(_('Could not remove the saved API key. Unlock the keyring and retry.'))
            except GLib.Error:
                raise CredentialError(_('Could not remove the saved API key. Unlock the keyring and retry.')) from None
