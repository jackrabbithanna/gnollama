"""Structured request formats and local, offline response validation."""
import json
import math

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from referencing.exceptions import Unresolvable


class InvalidSchema(ValueError):
    """A schema needs editing before a request can be sent."""


def parse_json(text):
    def constant(value):
        raise ValueError(_('Non-finite numbers are not valid JSON.'))

    def number(value):
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(_('JSON number is too large to format.'))
        return result

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(_('Duplicate JSON key: {0}').format(key))
            result[key] = value
        return result

    return json.loads(text, parse_constant=constant, parse_float=number, object_pairs_hook=pairs)


def schema_validator(schema):
    if not isinstance(schema, dict):
        raise ValueError(_('The schema must be a JSON object.'))
    validator_class = validators.validator_for(schema, default=None) if '$schema' in schema else Draft202012Validator
    if validator_class is None:
        raise ValueError(_('Unsupported JSON Schema version.'))
    try:
        validator_class.check_schema(schema)
        resource = Resource.from_contents(schema, default_specification=DRAFT202012)
        registry = Registry().with_resource('urn:gnollama:schema', resource).crawl()
        # Resolve references before sending, including references in optional fields.
        # Registry has no retrieval callback: imported schemas stay self-contained.
        def check_refs(resource, resolver):
            contents = resource.contents
            if isinstance(contents, dict):
                for key in ('$ref', '$dynamicRef', '$recursiveRef'):
                    if key in contents:
                        resolver.lookup(contents[key])
            for child in resource.subresources():
                check_refs(child, resolver.in_subresource(child))
        check_refs(resource, registry.resolver('urn:gnollama:schema').in_subresource(resource))
        return validator_class(schema, registry=registry)
    except SchemaError as exc:
        raise ValueError(_('Invalid schema: {0}').format(exc.message)) from exc
    except Unresolvable as exc:
        raise ValueError(_('Unresolved schema reference: {0}. Import a self-contained schema.').format(exc.ref)) from exc


def request_format(mode, schema_text=''):
    if mode == 'text':
        return None
    if mode == 'json':
        return 'json'
    if mode != 'schema':
        raise ValueError(_('Unknown output format.'))
    if not schema_text.strip():
        raise InvalidSchema(_('Paste or import a JSON schema, then click Apply before sending.'))
    try:
        schema = parse_json(schema_text)
    except (ValueError, RecursionError) as exc:
        raise InvalidSchema(_('Invalid schema JSON: {0}').format(exc)) from exc
    try:
        schema_validator(schema)
    except (ValueError, RecursionError) as exc:
        raise InvalidSchema(str(exc)) from exc
    return schema


def validate_response(text, output_format, status='complete'):
    if output_format is None:
        return None
    if status != 'complete':
        return {'status': 'incomplete'}
    try:
        instance = parse_json(text)
    except (ValueError, RecursionError) as exc:
        return {'status': 'invalid_json', 'message': str(exc)}
    if isinstance(output_format, dict):
        try:
            error = next(schema_validator(output_format).iter_errors(instance), None)
            if error is not None:
                path = '/' + '/'.join(str(p).replace('~', '~0').replace('/', '~1') for p in error.absolute_path)
                return {'status': 'schema_mismatch', 'path': path, 'message': error.message}
        except (ValueError, Unresolvable, RecursionError) as exc:
            return {'status': 'validation_error', 'message': str(exc)}
    return {'status': 'valid'}


def formatted_json(text):
    return json.dumps(parse_json(text), ensure_ascii=False, indent=2, allow_nan=False) + '\n'
