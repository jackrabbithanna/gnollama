"""Ubuntu's Inference Manager: one OpenAI-compatible endpoint for every model installed through it.

The server lists models by id alone. What each one can do comes from the catalogue of providers
that the service keeps on its machine. A provider serves a model under its own alias, which the
catalogue does not hold, so each installed provider's model manifests tell which model an alias is.
The manifests hold what the catalogue does, so where the catalogue cannot be read, as from a
sandbox, they stand in for it.
"""
import glob
import json
import os
import re
from dataclasses import dataclass
from gettext import gettext as _

from . import systemone
from .base import Capabilities
from .openai import OpenAICompatibleProvider, effort, fingerprint, image_url, table
from .transport import ProviderError, read_all, validate_url

LABEL = 'Ubuntu Inference Manager'
# Where the service on this machine keeps its copy of Canonical's catalogue. Any user may read it.
CATALOGUE = '/var/snap/inference/common/onboarded-snaps.json'
# Where installed provider snaps are mounted; each has `models/<catalogue name>/model.yaml`.
SNAPS = '/snap'
# A provider is llama.cpp behind the proxy. It ignores `reasoning_effort`; thinking is switched in the
# chat template.
THINKING = {False: False, True: True}
# The tokens one input to embed may hold: an embedding provider's server leaves llama.cpp's physical batch
# at its default, and refuses an input longer than one batch, whatever the model itself takes.
EMBEDDING_INPUT = 512
UNITS = {'K': 1024, 'M': 1024 ** 2, 'G': 1024 ** 3, 'T': 1024 ** 4}


def _extracted():
    """Marks text that is translated where it is displayed, not here."""
    _('Ubuntu Inference Manager')
    _('No models are installed on this server. Install one there with '
      '“inference install”, for example “sudo inference install gemma4”.')


def catalogue(path=CATALOGUE):
    """The catalogue's models by (provider, model name); empty where it is missing or not as expected."""
    try:
        with open(path, encoding='utf-8') as file:
            providers = json.load(file)
    except (OSError, ValueError):
        return {}
    found = {}
    for provider in providers if isinstance(providers, list) else ():
        snap = table(provider).get('snap')
        models = table(provider).get('models')
        if not isinstance(snap, str) or not isinstance(models, list):
            continue
        for model in models:
            if isinstance(table(model).get('name'), str):
                found[(snap, model['name'])] = dict(model, model_name=provider.get('model_name'))
    return found


def manifest(text, name):
    """A catalogue entry from a provider's model manifest: the same facts, under the catalogue's names."""
    def field(key):
        found = re.search(rf'^{key}:[ \t]*(\S+)[ \t]*$', text, re.MULTILINE)
        return found[1] if found else None
    listed = re.search(r'^capabilities:[ \t]*\n((?:[ \t]+-[^\n]*(?:\n|$))+)', text, re.MULTILINE)
    entry = {'name': name, 'capabilities': re.findall(r'-[ \t]*(\S+)', listed[1]) if listed else []}
    for key, value in (('quantization', field('quantization')), ('disk_size', field('disk-size'))):
        if value:
            entry[key] = value
    return entry


def installed(snap, root=SNAPS):
    """The models of the installed provider `snap`, from its manifests, by the alias each is served under.

    A manifest's `MODEL_NAME=` is the alias; a model whose manifest sets none is served under its own name.
    """
    found: dict[str, dict] = {}
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', snap):
        return found  # Not a snap name, so not a path to follow.
    for path in glob.glob(os.path.join(root, snap, 'current', 'models', '*', 'model.yaml')):
        name = os.path.basename(os.path.dirname(path))
        try:
            with open(path, encoding='utf-8') as file:
                text = file.read()
        except (OSError, UnicodeError):
            continue
        alias = re.search(r'^\s*-\s*MODEL_NAME=(\S+)\s*$', text, re.MULTILINE)
        found[alias[1] if alias else name] = manifest(text, name)
    return found


def split(model):
    """A listed model's provider and its own name: the id is `provider/name`."""
    provider, _slash, name = model['id'].partition('/')
    owner = model.get('owned_by')
    return (owner if isinstance(owner, str) and owner else provider), name


def named(entry):
    """The capability names a catalogue entry gives; any it gives that are not names are ignored."""
    listed = table(entry).get('capabilities')
    return {name for name in listed if isinstance(name, str)} if isinstance(listed, list) else set()


def disk_size(text):
    """Bytes from a size such as `540M`, or None."""
    found = re.fullmatch(r'(\d+(?:\.\d+)?)([KMGT])', text.strip()) if isinstance(text, str) else None
    return int(float(found[1]) * UNITS[found[2]]) if found else None


def details(entry):
    found = {'family': table(entry).get('model_name'), 'quantization_level': table(entry).get('quantization')}
    return {key: value for key, value in found.items() if isinstance(value, str) and value}


def embeds_only(entry):
    names = named(entry)
    return 'text-embedding' in names and 'text' not in names


def decides_only(entry):
    names = named(entry)
    return 'decision' in names and 'text' not in names


def describe(entry):
    """What `show_model` says of a model the catalogue lists."""
    names = named(entry)
    result: dict = {'details': details(entry),
                    'supports': {name: name in names for name in ('vision', 'tools', 'thinking')}}
    if embeds_only(entry):
        result['capabilities'] = ['embedding']
    if decides_only(entry):
        # A whole list: a decision model that does not say it sees images is sent none.
        result['capabilities'] = systemone.capabilities('vision' in names)
    if 'thinking' in names:
        result['thinking_levels'] = list(THINKING)
    size = disk_size(table(entry).get('disk_size'))
    return dict(result, size=size) if size else result


@dataclass(frozen=True)
class InferenceManagerProvider(OpenAICompatibleProvider):
    kind = 'inference_manager'
    label = default_name = name_example = LABEL
    default_url = url_example = 'http://localhost:8400'
    # The server has no key: the proxy drops an Authorization header.
    key_optional = False
    capabilities = Capabilities(embeddings=True, thinking_levels=True, structured_output=True, tools=True,
                                logprobs=True, sampling_extras=True, thinking_text=True,
                                # Choice, score and noul questions; a box question is refused.
                                decisions=True, decision_images=True)
    # The server ignores the `json_object` type; a schema is enforced.
    json_mode = {'type': 'json_schema', 'json_schema': {'name': 'response', 'schema': {'type': 'object'}}}
    empty_notice = ('No models are installed on this server. Install one there with '
                    '“inference install”, for example “sudo inference install gemma4”.')
    catalogue_path = CATALOGUE
    snaps_path = SNAPS

    def thinking_fields(self, thinking):
        return {'chat_template_kwargs': {'enable_thinking': effort(thinking, THINKING)}}

    def _explain(self, status, detail):
        if status == 404:
            return _('This model is no longer served. Its provider may be stopped or removed on the server.')
        if status == 502:
            return _('The model’s provider did not answer. It may still be loading, or it may be stopped.')
        return super()._explain(status, detail)

    def _entries(self):
        """A function giving a listed model's catalogue entry, or None where the catalogue does not list it."""
        known = catalogue(self.catalogue_path)
        manifests: dict = {}

        def entry(model):
            snap, served = split(model)
            if (snap, served) in known:
                return known[(snap, served)]
            if snap not in manifests:
                manifests[snap] = installed(snap, self.snaps_path)
            local = manifests[snap].get(served)
            if local is None:
                return None
            # The catalogue, where it lists the model, also names the provider; without it, the manifest says the rest.
            return known.get((snap, local['name']), local)
        return entry

    def _served(self, timeout, cancellable):
        entry = self._entries()
        listed = self._listed(lambda: self._address('/models'), 'data', 'id', timeout, cancellable)
        return [(model, entry(model)) for model in listed]

    def _tag(self, model, entry):
        result = {'name': model['id'], 'details': details(entry),
                  'digest': fingerprint(model['id'], table(entry).get('quantization'),
                                        table(entry).get('disk_size'))}
        if embeds_only(entry):
            # Said in the list, so a chat model list can leave it out before asking for details.
            result['capabilities'] = ['embedding']
        size = disk_size(table(entry).get('disk_size'))
        return dict(result, size=size) if size else result

    def list_models(self, timeout=10, cancellable=None):
        # A decision model takes no chat or embedding request.
        return [self._tag(model, entry) for model, entry in self._served(timeout, cancellable)
                if not decides_only(entry)]

    def list_embedding_models(self, timeout=10, cancellable=None):
        served = self._served(timeout, cancellable)
        if not any(entry is not None for _model, entry in served):
            # Nothing is known of any model, so none is ruled out.
            return [self._tag(model, entry) for model, entry in served]
        return [self._tag(model, entry) for model, entry in served if 'text-embedding' in named(entry)]

    def list_decision_models(self, timeout=10, cancellable=None):
        return [dict(self._tag(model, entry), capabilities=systemone.capabilities('vision' in named(entry)))
                for model, entry in self._served(timeout, cancellable) if decides_only(entry)]

    def decide(self, model, state, questions, images=None, keep_alive=None, timeout=120, cancellable=None):
        """Ask a decision model the named `questions` about `state`; returns the server's answer as it came."""
        if keep_alive is not None:
            raise ProviderError(_('This server does not accept: {0}.').format('keep_alive'))
        # Images go as data URLs; raw base64 is refused, and assets are ignored without an error.
        data = systemone.flat_body(systemone.request(model, state, questions), images, image_url)
        return systemone.post(self, lambda: self._address('/systemone'), data, timeout, cancellable)

    def embedding_context(self, name, timeout=10, cancellable=None):
        entry = self._entries()({'id': name})
        return EMBEDDING_INPUT if 'text-embedding' in named(entry) else None

    def show_model(self, name, timeout=10, cancellable=None):
        entry = self._entries()({'id': name})
        # Of a model the catalogue does not list, nothing is known but the settings the server takes.
        return describe(entry) if entry is not None else {'thinking_levels': list(THINKING)}

    def probe(self, cancellable=None):
        """Says so when the address answers but is not Inference Manager."""
        refused = ProviderError(_('This server is not Inference Manager. '
                                  'Use the OpenAI-Compatible Server type for it.'))
        try:
            with self.transport.response(lambda: validate_url(self.url).removesuffix('/v1') + '/v1/', None,
                                         'GET', 10, cancellable) as (stream, cancel):
                raw = read_all(stream, cancel)
        except ProviderError as exc:
            if exc.status is None:
                raise  # Nothing answered.
            raise refused from None
        try:
            answer = json.loads(raw)
        except (ValueError, UnicodeError):
            raise refused from None
        if not isinstance(table(answer).get('documentation-url'), str):
            raise refused
        return super().probe(cancellable)
