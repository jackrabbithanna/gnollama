import base64
import unittest
from dataclasses import dataclass
from types import SimpleNamespace

from src.image_generation import (ImageParameters, Parameter, check_request, extension, media_type,
                                  parameters_from_schema, parse_result, unavailable, with_defaults)
from src.providers.base import Capabilities, Provider, generates
from src.providers.ollama import OllamaCloudProvider, OllamaProvider
from src.records import ImageSettings, ModelTarget


# Shaped as OpenRouter's /api/v1/images/models lists them.
SCHEMA = {
    'aspect_ratio': {'type': 'enum', 'values': ['1:1', '16:9', '9:16', 'auto']},
    'resolution': {'type': 'enum', 'values': ['1K', '2K']},
    'n': {'type': 'range', 'min': 1, 'max': 4},
    'seed': {'type': 'boolean'},
    'input_references': {'type': 'range', 'min': 0, 'max': 2},
    'output_compression': {'type': 'range', 'min': 0, 'max': 100},
    'size': {'type': 'boolean'},
}
PNG = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'\0' * 24).decode()
JPEG = base64.b64encode(b'\xff\xd8\xff\xe0' + b'\0' * 24).decode()
WEBP = base64.b64encode(b'RIFF\0\0\0\0WEBPVP8 ' + b'\0' * 16).decode()
SVG = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').decode()
ALL = SimpleNamespace(image_generation=True, image_references=True)


class ParameterTests(unittest.TestCase):
    def test_reads_the_settings_offered_in_order(self):
        parameters = parameters_from_schema(SCHEMA)
        self.assertEqual([p.name for p in parameters.options], ['aspect_ratio', 'resolution', 'n', 'seed'])
        self.assertEqual(parameters.get('aspect_ratio'), Parameter('aspect_ratio', 'choice', ('1:1', '16:9', '9:16', 'auto')))
        self.assertEqual(parameters.get('n'), Parameter('n', 'range', low=1, high=4))
        self.assertEqual(parameters.get('seed').kind, 'integer')
        self.assertEqual(parameters.references, 2)
        self.assertEqual(parameters.count(), (1, 4))

    def test_leaves_out_what_cannot_be_read(self):
        parameters = parameters_from_schema({
            'aspect_ratio': {'type': 'enum', 'values': []}, 'quality': {'type': 'enum', 'values': [1, '', None]},
            'n': {'type': 'range', 'min': 3, 'max': 2}, 'seed': 'yes', 'background': {'type': 'boolean'},
            'input_references': {'type': 'range', 'min': 0, 'max': 0}})
        self.assertEqual(parameters, ImageParameters())
        self.assertEqual(parameters.count(), (1, 1))
        self.assertEqual(parameters_from_schema(None), ImageParameters())
        self.assertEqual(parameters_from_schema({'n': {'type': 'range', 'min': 0, 'max': 2}}).options, ())

    def test_references_are_withheld_where_the_host_sends_none(self):
        self.assertEqual(parameters_from_schema(SCHEMA, references=False).references, 0)
        self.assertTrue(parameters_from_schema(SCHEMA, streaming=True).streaming)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.parameters = parameters_from_schema(SCHEMA)

    def check(self, prompt='a cat', references=(), options=None, capabilities=ALL):
        return check_request(prompt, references, options or {}, capabilities, self.parameters)

    def test_a_valid_request_passes(self):
        self.assertEqual(self.check(options={'aspect_ratio': '16:9', 'n': 4, 'seed': 0}, references=[PNG, PNG]), '')

    def test_a_host_without_image_generation_is_refused(self):
        self.assertEqual(self.check(capabilities=Capabilities()), 'This server does not offer image generation.')
        self.assertEqual(unavailable(None), 'This server does not offer image generation.')
        self.assertEqual(unavailable(ALL), '')

    def test_prompt_is_required(self):
        self.assertEqual(self.check('  \n'), 'Enter a prompt.')
        self.assertEqual(self.check(None), 'Enter a prompt.')

    def test_references_are_limited(self):
        self.assertEqual(self.check(references=[PNG] * 3), 'This model takes at most 2 reference images.')
        self.parameters = ImageParameters()
        self.assertEqual(self.check(references=[PNG]), 'This model does not take reference images.')
        self.parameters = parameters_from_schema(SCHEMA)
        self.assertEqual(self.check(references=[PNG], capabilities=SimpleNamespace(
            image_generation=True, image_references=False)), 'This model does not take reference images.')

    def test_settings_must_be_offered_and_in_range(self):
        self.assertEqual(self.check(options={'quality': 'high'}), 'This model does not take the setting Quality.')
        self.assertEqual(self.check(options={'output_compression': 5}),
                         'This model does not take the setting output_compression.')
        for options in ({'aspect_ratio': '4:3'}, {'n': 5}, {'n': 0}, {'n': True}, {'n': 2.0},
                        {'seed': -1}, {'seed': 2 ** 32}, {'seed': '7'}):
            with self.subTest(options=options):
                self.assertIn('does not take that value', self.check(options=options))


class DefaultTests(unittest.TestCase):
    def test_png_is_chosen_where_offered_and_nothing_was(self):
        offered = parameters_from_schema({'output_format': {'type': 'enum', 'values': ['webp', 'png']}})
        self.assertEqual(with_defaults({'n': 2}, offered), {'n': 2, 'output_format': 'png'})
        self.assertEqual(with_defaults({'output_format': 'webp'}, offered), {'output_format': 'webp'})
        svg = parameters_from_schema({'output_format': {'type': 'enum', 'values': ['svg']}})
        self.assertEqual(with_defaults(None, svg), {})
        self.assertEqual(with_defaults({}, ImageParameters()), {})


class ResultTests(unittest.TestCase):
    def test_images_api_answer(self):
        result = parse_result({'created': 1, 'data': [{'b64_json': PNG, 'media_type': 'image/png'},
                                                      {'b64_json': JPEG}, {'url': 'https://cdn/x.png'}],
                               'usage': {'cost': 0.04, 'completion_tokens': 10}})
        self.assertEqual([image.media_type for image in result.images], ['image/png', 'image/jpeg'])
        self.assertEqual(result.images[0].data, PNG)
        self.assertEqual(result.cost, 0.04)
        self.assertEqual(result.extra, {'created': 1, 'usage': {'cost': 0.04, 'completion_tokens': 10},
                                        'links': ['https://cdn/x.png']})

    def test_data_urls_in_a_data_list(self):
        result = parse_result({'data': [{'url': f'data:image/webp;base64,{WEBP}'}]})
        self.assertEqual((result.images[0].data, result.images[0].media_type), (WEBP, 'image/webp'))

    def test_chat_answer_with_images_and_text(self):
        result = parse_result({'choices': [{'message': {'content': ' Here it is. ', 'images': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{PNG}'}}]}}],
            'provider_metadata': {'gateway': {'cost': '0.039'}}})
        self.assertEqual(result.text, 'Here it is.')
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.cost, 0.039)

    def test_gateway_cost_in_camel_case(self):
        # As Vercel's images API answered on 2026-11-24; the image service's own figure is in credits.
        result = parse_result({'data': [{'b64_json': JPEG}], 'providerMetadata': {
            'blackForestLabs': {'images': [{'cost': 0.9}]}, 'gateway': {'cost': '0.009000000000000001'}}})
        self.assertAlmostEqual(result.cost, 0.009)

    def test_an_answer_without_images_keeps_its_text(self):
        result = parse_result('{"choices": [{"message": {"content": "I cannot draw that."}}]}')
        self.assertEqual((result.images, result.text), ((), 'I cannot draw that.'))

    def test_what_is_not_an_answer(self):
        for raw in (None, [], 'nonsense', '[1]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_result(raw)
        result = parse_result({'data': ['x', {'b64_json': ''}, {'url': 'data:image/png,notbase64'}],
                               'usage': {'cost': -1}, 'choices': 'x'})
        self.assertEqual((result.images, result.cost), ((), None))

    def test_media_types_and_extensions(self):
        self.assertEqual([media_type(data) for data in (PNG, JPEG, WEBP, SVG, 'not base64!')],
                         ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml', 'image/png'])
        self.assertEqual([extension(kind) for kind in ('image/jpeg', 'image/svg+xml', 'image/x-unknown')],
                         ['jpg', 'svg', 'png'])


@dataclass(frozen=True)
class Imaging(Provider):
    capabilities = Capabilities(image_generation=True, image_references=True)


class ProviderTests(unittest.TestCase):
    def test_parameters_never_exceed_the_provider(self):
        entry = {'name': 'm', 'capabilities': ['image'], 'image_parameters': SCHEMA, 'image_streaming': True}
        parameters = Imaging('http://x').image_parameters(entry)
        self.assertEqual((parameters.references, parameters.streaming), (2, True))
        self.assertEqual(Imaging('http://x').image_parameters(None), ImageParameters())
        self.assertEqual(OllamaProvider('http://x').image_parameters(entry), ImageParameters())

    def test_no_ollama_type_generates_images(self):
        for kind in (OllamaProvider, OllamaCloudProvider):
            with self.subTest(kind=kind.kind):
                self.assertFalse(kind.capabilities.image_generation)
                with self.assertRaises(NotImplementedError):
                    kind('http://x').generate_image('m', 'p', {})
                with self.assertRaises(NotImplementedError):
                    kind('http://x').list_image_models()

    def test_image_models_are_not_chat_models(self):
        self.assertFalse(generates({'capabilities': ['image']}))
        self.assertTrue(generates({'capabilities': ['completion', 'vision', 'image']}))
        self.assertTrue(generates({}))

    def test_both_combines_image_flags(self):
        combined = Imaging.capabilities.both(Capabilities(image_generation=True))
        self.assertEqual((combined.image_generation, combined.image_references), (True, False))


class SettingsTests(unittest.TestCase):
    def test_round_trip(self):
        settings = ImageSettings(ModelTarget('h', 'https://openrouter.ai/api', 'm', 'Router'), {'n': 2})
        self.assertEqual(ImageSettings.from_stored(settings.to_stored()), settings)
        self.assertEqual(ImageSettings.from_stored({'options': 'bad', 'target': 3}), ImageSettings())
        self.assertEqual(ImageSettings.from_stored(None), ImageSettings())


if __name__ == '__main__':
    unittest.main()
