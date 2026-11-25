"""Image documents in Knowledge: provider wire formats, storage, indexing, search, and chat use."""
import base64
import copy
import tempfile
import unittest
from types import SimpleNamespace

from gi.repository import Gdk, GLib

from src.providers import OllamaProvider, ProviderError
from src.providers.lmstudio import LMStudioProvider
from src.providers.openai import OpenAICompatibleProvider
from src.providers.openai_platform import OpenAIProvider
from src.providers.vercel import VercelProvider
from src.providers.openrouter import OpenRouterProvider, describe_embedding
from src.app_context import AppContext
from src.knowledge import (IMAGE_SIDE, augmented_messages, extract_document, image_mime,
                           make_document, new_config)
from src.session import retrieved_images

from provider_fixture import provider_patch
from test_transport import Server as OllamaServer
from test_openai import Server as OpenAIServer, vectors
from test_ui import pump_until

PIXEL = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==')
TAG = {'name': 'embeddinggemma2:latest', 'digest': 'digest-gemma'}
HOST = 'http://embed:11434'


def png(width, height):
    texture = Gdk.MemoryTexture.new(width, height, Gdk.MemoryFormat.R8G8B8A8,
                                    GLib.Bytes.new(b'\x10\x80\xf0\xff' * width * height), width * 4)
    return bytes(texture.save_to_png_bytes().get_data())


class ProviderImageTests(unittest.TestCase):
    def test_ollama_sends_images_as_they_are_beside_text(self):
        fixture = OllamaServer('json')
        self.addCleanup(fixture.close)
        fixture.server.payload = {'embeddings': [[1., 0.], [0., 1.]]}
        OllamaProvider(fixture.host).embed('m', ['text', {'image': 'aGk='}])
        self.assertEqual(fixture.server.received[-1][1]['input'], ['text', {'image': 'aGk='}])
        for bad in ({'image': ''}, {'image': 'x', 'audio': 'y'}, {'video': 'x'}):
            with self.assertRaises(ProviderError):
                OllamaProvider(fixture.host).embed('m', [bad])

    def test_openai_compatible_sends_an_image_url_content_part(self):
        fixture = OpenAIServer()
        self.addCleanup(fixture.close)
        fixture.server.embeddings = vectors([1, 0], [0, 1])
        image = base64.b64encode(PIXEL).decode()
        OpenAICompatibleProvider(fixture.host).embed('m', ['text', {'image': image}])
        self.assertEqual(fixture.server.received[-1][1]['input'], [
            'text', {'content': [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + image}}]}])

    def test_providers_without_image_embeddings_refuse_images(self):
        for kind in (LMStudioProvider, VercelProvider, OpenAIProvider):
            provider = kind(kind.fixed_url or 'http://127.0.0.1:9')
            self.assertFalse(provider.capabilities.image_embeddings)
            self.assertIs(provider.embeds_images('m'), False)
            with self.assertRaises(ProviderError):
                provider.embed('m', [{'image': 'aGk='}])

    def test_openrouter_says_which_embedders_see_images(self):
        self.assertTrue(OpenRouterProvider.capabilities.image_embeddings)
        seeing = {'id': 'a', 'architecture': {'input_modalities': ['text', 'image']}}
        self.assertEqual(describe_embedding(seeing)['capabilities'], ['embedding', 'vision'])
        self.assertEqual(describe_embedding({'id': 'b'})['capabilities'], ['embedding'])

    def test_ollama_asks_the_models_own_details(self):
        with provider_patch('show_model', return_value={'capabilities': ['embedding', 'vision']}):
            self.assertIs(OllamaProvider(HOST).embeds_images('m'), True)
        with provider_patch('show_model', return_value={'capabilities': ['embedding']}):
            self.assertIs(OllamaProvider(HOST).embeds_images('m'), False)


class ExtractionTests(unittest.TestCase):
    def test_images_are_detected_reduced_and_kept_as_png(self):
        self.assertEqual(image_mime(PIXEL), 'image/png')
        self.assertEqual(image_mime(b'RIFF0000WEBPVP8 '), 'image/webp')
        self.assertIsNone(image_mime(b'%PDF-1.7'))
        document, warnings = extract_document('wide.png', png(IMAGE_SIDE * 2, 10))
        self.assertEqual(warnings, [])
        self.assertEqual((document['media_type'], document['text'], document['pages']), ('image/png', '', []))
        self.assertEqual(document['media']['width'], IMAGE_SIDE)
        self.assertEqual(image_mime(document['media']['data']), 'image/png')
        with self.assertRaisesRegex(ValueError, 'cannot be read'):
            extract_document('broken.png', PIXEL[:20])


class ImageKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.context = AppContext(self.temp.name)
        self.storage = self.context.storage
        self.storage.add_host('Embedding host', HOST)
        self.knowledge = self.storage.db.knowledge

    def tearDown(self):
        self.context.knowledge.cancel_all()
        pump_until(lambda: self.context.knowledge.idle and self.storage.writer.idle)
        self.context.knowledge.shutdown()
        self.context.shutdown()
        self.temp.cleanup()

    def image(self, caption=''):
        document, _warnings = extract_document('pixel.png', PIXEL)
        document['text'] = caption
        self.knowledge.add_knowledge_document(document)
        return document

    def build(self, document, config, details):
        seen = []

        def embed(host, model, input, **kwargs):
            seen.append(input)
            return {'embeddings': [[1, 0] if isinstance(item, dict) else [0, 1] for item in input]}
        with provider_patch('list_models', return_value=[TAG]), provider_patch('show_model', return_value=details), \
                provider_patch('embed', side_effect=embed):
            job = self.context.knowledge.create_index(document['id'], HOST, TAG['name'], config)
            pump_until(lambda: job['done'] and self.storage.writer.idle, timeout=8)
        return job, seen

    def test_storage_dedupes_by_image_and_keeps_the_caption_editable(self):
        document = self.image('A red pixel')
        stored = self.knowledge.knowledge_document(document['id'])
        self.assertEqual((stored['media_type'], stored['text']), ('image/png', 'A red pixel'))
        self.assertEqual(self.knowledge.knowledge_media(document['id'])['data'], document['media']['data'])
        self.assertEqual(self.knowledge.document_page()[0]['media_type'], 'image/png')
        self.knowledge.set_document_caption(document['id'], 'Changed')
        self.assertEqual(self.knowledge.knowledge_document(document['id'])['text'], 'Changed')
        text = make_document('Text', 'plain text')
        self.knowledge.add_knowledge_document(text)
        self.assertIsNone(self.knowledge.knowledge_media(text['id']))
        with self.assertRaises(ValueError):
            self.knowledge.set_document_caption(text['id'], 'nope')
        self.knowledge.delete_knowledge_document(document['id'])
        self.assertIsNone(self.knowledge.knowledge_media(document['id']))

    def test_an_image_is_one_chunk_embedded_without_the_text_prefix_and_found_by_text(self):
        document = self.image('A red pixel')
        text = make_document('Notes', 'Plain notes about something else.')
        self.knowledge.add_knowledge_document(text)
        config = new_config(TAG['name'], TAG['digest'], 'embeddinggemma')
        details = {'capabilities': ['embedding', 'vision']}
        job, seen = self.build(document, config, details)
        self.assertIsNone(job['error'])
        self.assertEqual(seen, [[{'image': base64.b64encode(document['media']['data']).decode()}]])
        index = self.knowledge.knowledge_indexes(document_id=document['id'])[0]
        self.assertEqual((index['status'], index['chunks']), ('complete', 1))
        second, _seen = self.build(text, config, details)
        self.assertIsNone(second['error'])
        selection = {i['id']: None for i in self.knowledge.knowledge_indexes()}
        # The caption changes after embedding; the hit carries the current one.
        self.knowledge.set_document_caption(document['id'], 'A much longer red pixel caption')
        config = self.knowledge.embedding_config(config['id'])
        hits = self.knowledge.search_knowledge(config['id'], selection, [1, 0], count=2)
        self.assertEqual(hits[0]['media'], {'type': 'image/png', 'document_id': document['id']})
        self.assertEqual(hits[0]['text'], 'A much longer red pixel caption')
        self.assertNotIn('media', hits[1])
        snapshot = dict(hits=hits)
        images = self.context.knowledge.hit_images(snapshot)
        self.assertEqual(images, [base64.b64encode(document['media']['data']).decode()])

    def test_a_model_that_does_not_see_images_fails_only_that_build(self):
        document = self.image()
        config = new_config(TAG['name'], TAG['digest'])
        job, seen = self.build(document, config, {'capabilities': ['embedding']})
        self.assertRegex(job['error'], 'does not accept images')
        self.assertEqual(seen, [])
        self.assertEqual(self.knowledge.knowledge_indexes(document_id=document['id'])[0]['status'], 'failed')


class ChatUseTests(unittest.TestCase):
    SNAPSHOT = dict(hits=[
        dict(title='Photo', text='A red pixel', media={'type': 'image/png', 'document_id': 'd'}),
        dict(title='Notes', text='Plain notes')])

    def test_image_hits_are_labelled_and_their_images_follow_the_users(self):
        messages = [{'role': 'user', 'content': 'What colour?', 'images': ['mine']}]
        sent = augmented_messages(messages, self.SNAPSHOT, ['found'])
        self.assertIn('[S1] Image: Photo\nA red pixel', sent[-1]['content'])
        self.assertIn('[S2] Notes\nPlain notes', sent[-1]['content'])
        self.assertEqual(sent[-1]['images'], ['mine', 'found'])
        self.assertIn('Images attached after the question', sent[0]['content'])
        plain = augmented_messages(messages, self.SNAPSHOT)
        self.assertEqual(plain[-1]['images'], ['mine'])
        self.assertNotIn('Images attached', plain[0]['content'])
        self.assertEqual(messages[0]['images'], ['mine'])

    def test_retrieved_images_go_only_to_a_model_that_sees_them(self):
        def load(snapshot):
            return ['found']

        for support, expected in ((True, ['found']), (False, None), (None, None)):
            state = SimpleNamespace(retrieval=copy.deepcopy(self.SNAPSHOT), image_support=support,
                                    reference_images=load)
            self.assertEqual(retrieved_images(state), expected)
        state = SimpleNamespace(retrieval=None, image_support=True, reference_images=load)
        self.assertIsNone(retrieved_images(state))


if __name__ == '__main__':
    unittest.main()
