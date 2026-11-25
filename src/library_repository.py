"""Knowledge persistence interface used by library controllers."""

class LibraryRepository:
    def __init__(self, database, submit, hosts):
        self._database = database
        # Ordered writes and the configured hosts, without the rest of storage.
        self.submit = submit
        self.hosts = hosts

    def add_embedding_config(self, *args, **kwargs):
        return self._database.knowledge.add_embedding_config(*args, **kwargs)

    def add_knowledge_document(self, *args, **kwargs):
        return self._database.knowledge.add_knowledge_document(*args, **kwargs)

    def begin_knowledge_index(self, *args, **kwargs):
        return self._database.knowledge.begin_knowledge_index(*args, **kwargs)

    def check_collection_sources(self, *args, **kwargs):
        return self._database.knowledge.check_collection_sources(*args, **kwargs)

    def collection_documents(self, *args, **kwargs):
        return self._database.knowledge.collection_documents(*args, **kwargs)

    def create_comparison(self, *args, **kwargs):
        return self._database.workspace.create_comparison(*args, **kwargs)

    def create_decision_run(self, *args, **kwargs):
        return self._database.decisions.create_decision_run(*args, **kwargs)

    def create_image_run(self, *args, **kwargs):
        return self._database.images.create_image_run(*args, **kwargs)

    def create_knowledge_collection(self, *args, **kwargs):
        return self._database.knowledge.create_knowledge_collection(*args, **kwargs)

    def create_model_conversation(self, *args, **kwargs):
        return self._database.model_conversations.create_model_conversation(*args, **kwargs)

    def decision_runs(self, *args, **kwargs):
        return self._database.decisions.decision_runs(*args, **kwargs)

    def delete_knowledge_collection(self, *args, **kwargs):
        return self._database.knowledge.delete_knowledge_collection(*args, **kwargs)

    def delete_knowledge_document(self, *args, **kwargs):
        return self._database.knowledge.delete_knowledge_document(*args, **kwargs)

    def delete_knowledge_index(self, *args, **kwargs):
        return self._database.knowledge.delete_knowledge_index(*args, **kwargs)

    def delete_model_embeddings(self, *args, **kwargs):
        return self._database.knowledge.delete_model_embeddings(*args, **kwargs)

    def discard_pending_model_conversation_attempt(self, *args, **kwargs):
        return self._database.model_conversations.discard_pending_model_conversation_attempt(*args, **kwargs)

    def document_page(self, *args, **kwargs):
        return self._database.knowledge.document_page(*args, **kwargs)

    def embedding_config(self, *args, **kwargs):
        return self._database.knowledge.embedding_config(*args, **kwargs)

    def embedding_configs(self, *args, **kwargs):
        return self._database.knowledge.embedding_configs(*args, **kwargs)

    def finish_comparison_target(self, *args, **kwargs):
        return self._database.workspace.finish_comparison_target(*args, **kwargs)

    def finish_decision_run(self, *args, **kwargs):
        return self._database.decisions.finish_decision_run(*args, **kwargs)

    def finish_image_run(self, *args, **kwargs):
        return self._database.images.finish_image_run(*args, **kwargs)

    def finish_knowledge_index(self, *args, **kwargs):
        return self._database.knowledge.finish_knowledge_index(*args, **kwargs)

    def finish_model_conversation_attempt(self, *args, **kwargs):
        return self._database.model_conversations.finish_model_conversation_attempt(*args, **kwargs)

    def image_runs(self, *args, **kwargs):
        return self._database.images.image_runs(*args, **kwargs)

    def import_collection_document(self, *args, **kwargs):
        return self._database.knowledge.import_collection_document(*args, **kwargs)

    def knowledge_chunk_ids(self, *args, **kwargs):
        return self._database.knowledge.knowledge_chunk_ids(*args, **kwargs)

    def knowledge_chunk_page(self, *args, **kwargs):
        return self._database.knowledge.knowledge_chunk_page(*args, **kwargs)

    def knowledge_collection(self, *args, **kwargs):
        return self._database.knowledge.knowledge_collection(*args, **kwargs)

    def knowledge_collections(self, *args, **kwargs):
        return self._database.knowledge.knowledge_collections(*args, **kwargs)

    def knowledge_document(self, *args, **kwargs):
        return self._database.knowledge.knowledge_document(*args, **kwargs)

    def knowledge_documents(self, *args, **kwargs):
        return self._database.knowledge.knowledge_documents(*args, **kwargs)

    def knowledge_indexes(self, *args, **kwargs):
        return self._database.knowledge.knowledge_indexes(*args, **kwargs)

    def knowledge_vector(self, *args, **kwargs):
        return self._database.knowledge.knowledge_vector(*args, **kwargs)

    def model_embedding_usage(self, *args, **kwargs):
        return self._database.knowledge.model_embedding_usage(*args, **kwargs)

    def prepare_collection_indexes(self, *args, **kwargs):
        return self._database.knowledge.prepare_collection_indexes(*args, **kwargs)

    def remove_collection_document(self, *args, **kwargs):
        return self._database.knowledge.remove_collection_document(*args, **kwargs)

    def rename_knowledge_collection(self, *args, **kwargs):
        return self._database.knowledge.rename_knowledge_collection(*args, **kwargs)

    def rename_knowledge_document(self, *args, **kwargs):
        return self._database.knowledge.rename_knowledge_document(*args, **kwargs)

    def save_embedding_batch(self, *args, **kwargs):
        return self._database.knowledge.save_embedding_batch(*args, **kwargs)

    def save_web_document(self, *args, **kwargs):
        return self._database.knowledge.save_web_document(*args, **kwargs)

    def search_knowledge(self, *args, **kwargs):
        return self._database.knowledge.search_knowledge(*args, **kwargs)

    def set_model_conversation_status(self, *args, **kwargs):
        return self._database.model_conversations.set_model_conversation_status(*args, **kwargs)

    def source_collections(self, *args, **kwargs):
        return self._database.knowledge.source_collections(*args, **kwargs)

    def start_comparison_target(self, *args, **kwargs):
        return self._database.workspace.start_comparison_target(*args, **kwargs)

    def start_model_conversation_attempt(self, *args, **kwargs):
        return self._database.model_conversations.start_model_conversation_attempt(*args, **kwargs)

    def ungrouped_document_ids(self, *args, **kwargs):
        return self._database.knowledge.ungrouped_document_ids(*args, **kwargs)

    def update_collection_endpoint(self, *args, **kwargs):
        return self._database.knowledge.update_collection_endpoint(*args, **kwargs)

    def web_document(self, *args, **kwargs):
        return self._database.knowledge.web_document(*args, **kwargs)
