"""Check native/Flatpak lock agreement without contacting a package index."""
import json
from pathlib import Path
import re

root = Path(__file__).resolve().parents[1]
pins = dict(re.findall(r'^([\w-]+)==([^\s]+)$', (root / 'requirements.lock').read_text(), re.M))
pins = {name.replace('_', '-').lower(): value for name, value in pins.items()}
manifest = json.loads((root / 'io.github.jackrabbithanna.Gnollama.json').read_text())
seen = set()
for module in manifest['modules']:
    for source in module.get('sources', []):
        filename = source.get('url', '').rsplit('/', 1)[-1]
        match = re.match(r'([\w]+)-([0-9][^-]*?)(?:-|\.tar\.gz$)', filename)
        if match:
            name, version = match.groups()
            name = name.replace('_', '-').lower()
            assert pins.get(name) == version, f'{name}: lock={pins.get(name)}, Flatpak={version}'
            seen.add(name)
assert seen == set(pins), f'Packages missing from Flatpak sources: {set(pins) - seen}'
print(f'{len(pins)} dependency pins agree')
