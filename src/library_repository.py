"""Knowledge persistence interface used by library controllers."""

class LibraryRepository:
    def __init__(self, database, submit, hosts):
        self._database = database
        # Ordered writes and the configured hosts, without the rest of storage.
        self.submit = submit
        self.hosts = hosts

    def add_embedding_config(self, *args, **kwargs):
        return self._database.add_embedding_config(*args, **kwargs)

    def add_knowledge_document(self, *args, **kwargs):
        return self._database.add_knowledge_document(*args, **kwargs)

    def begin_knowledge_index(self, *args, **kwargs):
        return self._database.begin_knowledge_index(*args, **kwargs)

    def check_collection_sources(self, *args, **kwargs):
        return self._database.check_collection_sources(*args, **kwargs)

    def collection_documents(self, *args, **kwargs):
        return self._database.collection_documents(*args, **kwargs)

    def create_comparison(self, *args, **kwargs):
        return self._database.create_comparison(*args, **kwargs)

    def create_knowledge_collection(self, *args, **kwargs):
        return self._database.create_knowledge_collection(*args, **kwargs)

    def create_model_conversation(self, *args, **kwargs):
        return self._database.create_model_conversation(*args, **kwargs)

    def delete_knowledge_collection(self, *args, **kwargs):
        return self._database.delete_knowledge_collection(*args, **kwargs)

    def delete_knowledge_document(self, *args, **kwargs):
        return self._database.delete_knowledge_document(*args, **kwargs)

    def delete_knowledge_index(self, *args, **kwargs):
        return self._database.delete_knowledge_index(*args, **kwargs)

    def delete_model_embeddings(self, *args, **kwargs):
        return self._database.delete_model_embeddings(*args, **kwargs)

    def discard_pending_model_conversation_attempt(self, *args, **kwargs):
        return self._database.discard_pending_model_conversation_attempt(*args, **kwargs)

    def document_page(self, *args, **kwargs):
        return self._database.document_page(*args, **kwargs)

    def embedding_config(self, *args, **kwargs):
        return self._database.embedding_config(*args, **kwargs)

    def embedding_configs(self, *args, **kwargs):
        return self._database.embedding_configs(*args, **kwargs)

    def finish_comparison_target(self, *args, **kwargs):
        return self._database.finish_comparison_target(*args, **kwargs)

    def finish_knowledge_index(self, *args, **kwargs):
        return self._database.finish_knowledge_index(*args, **kwargs)

    def finish_model_conversation_attempt(self, *args, **kwargs):
        return self._database.finish_model_conversation_attempt(*args, **kwargs)

    def import_collection_document(self, *args, **kwargs):
        return self._database.import_collection_document(*args, **kwargs)

    def knowledge_chunk_ids(self, *args, **kwargs):
        return self._database.knowledge_chunk_ids(*args, **kwargs)

    def knowledge_chunk_page(self, *args, **kwargs):
        return self._database.knowledge_chunk_page(*args, **kwargs)

    def knowledge_collection(self, *args, **kwargs):
        return self._database.knowledge_collection(*args, **kwargs)

    def knowledge_collections(self, *args, **kwargs):
        return self._database.knowledge_collections(*args, **kwargs)

    def knowledge_document(self, *args, **kwargs):
        return self._database.knowledge_document(*args, **kwargs)

    def knowledge_documents(self, *args, **kwargs):
        return self._database.knowledge_documents(*args, **kwargs)

    def knowledge_indexes(self, *args, **kwargs):
        return self._database.knowledge_indexes(*args, **kwargs)

    def knowledge_vector(self, *args, **kwargs):
        return self._database.knowledge_vector(*args, **kwargs)

    def model_embedding_usage(self, *args, **kwargs):
        return self._database.model_embedding_usage(*args, **kwargs)

    def prepare_collection_indexes(self, *args, **kwargs):
        return self._database.prepare_collection_indexes(*args, **kwargs)

    def remove_collection_document(self, *args, **kwargs):
        return self._database.remove_collection_document(*args, **kwargs)

    def rename_knowledge_collection(self, *args, **kwargs):
        return self._database.rename_knowledge_collection(*args, **kwargs)

    def rename_knowledge_document(self, *args, **kwargs):
        return self._database.rename_knowledge_document(*args, **kwargs)

    def save_embedding_batch(self, *args, **kwargs):
        return self._database.save_embedding_batch(*args, **kwargs)

    def save_web_document(self, *args, **kwargs):
        return self._database.save_web_document(*args, **kwargs)

    def search_knowledge(self, *args, **kwargs):
        return self._database.search_knowledge(*args, **kwargs)

    def set_model_conversation_status(self, *args, **kwargs):
        return self._database.set_model_conversation_status(*args, **kwargs)

    def source_collections(self, *args, **kwargs):
        return self._database.source_collections(*args, **kwargs)

    def start_comparison_target(self, *args, **kwargs):
        return self._database.start_comparison_target(*args, **kwargs)

    def start_model_conversation_attempt(self, *args, **kwargs):
        return self._database.start_model_conversation_attempt(*args, **kwargs)

    def ungrouped_document_ids(self, *args, **kwargs):
        return self._database.ungrouped_document_ids(*args, **kwargs)

    def update_collection_endpoint(self, *args, **kwargs):
        return self._database.update_collection_endpoint(*args, **kwargs)

    def web_document(self, *args, **kwargs):
        return self._database.web_document(*args, **kwargs)
