"""Knowledge persistence interface used by library controllers."""

class LibraryRepository:
    def __init__(self, database):
        self._database = database

    def add_knowledge_document(self, *args, **kwargs):
        return self._database.add_knowledge_document(*args, **kwargs)

    def collection_documents(self, *args, **kwargs):
        return self._database.collection_documents(*args, **kwargs)

    def create_comparison(self, *args, **kwargs):
        return self._database.create_comparison(*args, **kwargs)

    def delete_knowledge_collection(self, *args, **kwargs):
        return self._database.delete_knowledge_collection(*args, **kwargs)

    def delete_knowledge_document(self, *args, **kwargs):
        return self._database.delete_knowledge_document(*args, **kwargs)

    def delete_knowledge_index(self, *args, **kwargs):
        return self._database.delete_knowledge_index(*args, **kwargs)

    def document_page(self, *args, **kwargs):
        return self._database.document_page(*args, **kwargs)

    def embedding_config(self, *args, **kwargs):
        return self._database.embedding_config(*args, **kwargs)

    def embedding_configs(self, *args, **kwargs):
        return self._database.embedding_configs(*args, **kwargs)

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

    def remove_collection_document(self, *args, **kwargs):
        return self._database.remove_collection_document(*args, **kwargs)

    def rename_knowledge_collection(self, *args, **kwargs):
        return self._database.rename_knowledge_collection(*args, **kwargs)

    def rename_knowledge_document(self, *args, **kwargs):
        return self._database.rename_knowledge_document(*args, **kwargs)

    def source_collections(self, *args, **kwargs):
        return self._database.source_collections(*args, **kwargs)

    def ungrouped_document_ids(self, *args, **kwargs):
        return self._database.ungrouped_document_ids(*args, **kwargs)

    def update_collection_endpoint(self, *args, **kwargs):
        return self._database.update_collection_endpoint(*args, **kwargs)

    def web_document(self, *args, **kwargs):
        return self._database.web_document(*args, **kwargs)
