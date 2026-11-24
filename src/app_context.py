"""The application's services, created together and shut down in order."""
from .credentials import CredentialStore
from .knowledge import KnowledgeService
from .providers import ProviderRegistry, Transport
from .services import Services
from .storage import ChatStorage


class AppContext:
    """Peers handed to windows and controllers; none of them owns another."""

    def __init__(self, storage_dir=None, progress=None, credentials=None):
        self.credentials = credentials if credentials is not None else CredentialStore()
        self.storage = ChatStorage(storage_dir, progress=progress, credentials=self.credentials)
        self.services = Services()
        self.transport = Transport()
        self.providers = ProviderRegistry(self.transport, self.credentials)
        self.storage.hosts_changed = self.services.catalog.invalidate
        self.knowledge = KnowledgeService(self.storage.library, self.services, self.providers)

    def connection(self, host):
        """Snapshot a host's destination without retrieving its secret."""
        return self.providers.connection(host)

    def capabilities(self, host):
        return self.providers.capabilities(host)

    @property
    def idle(self):
        return self.services.idle and self.knowledge.idle and not self.storage.pending_callbacks

    def cancel_requests(self):
        """Stop active work and keep queued work from starting."""
        self.knowledge.cancel_all()
        self.transport.cancel_all()

    def resume_requests(self):
        self.knowledge.closed = False
        self.transport.resume()

    def shutdown(self, wait=False):
        """Close everything; the writer refuses while changes are unsaved."""
        self.knowledge.cancel_all()
        if not self.storage.writer.closed:
            self.storage.writer.shutdown()
        self.knowledge.shutdown()
        self.services.shutdown(wait=wait)
        # Work still finishing on a worker opens a connection of its own.
        self.storage.close()
