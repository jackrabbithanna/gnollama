# ollama.py
#
# Copyright 2025 Jackrabbithanna
#
# SPDX-License-Identifier: GPL-3.0-or-later

import json
import urllib.request

def fetch_models(host):
    """
    Fetches the list of available models from the Ollama host.
    """
    url = f"{host}/api/tags"
    try:
        with urllib.request.urlopen(url) as response:
            result = json.loads(response.read().decode('utf-8'))
            return [model['name'] for model in result.get('models', [])]
    except Exception as e:
        print(f"Failed to fetch models: {e}")
        return []

def generate(host, model, prompt, system=None, options=None, thinking=None, logprobs=False, top_logprobs=None):
    """
    Generator that streams responses from the Ollama Generate API.
    """
    url = f"{host}/api/generate"
    
    data = {
        "model": model,
        "prompt": prompt,
        "stream": True
    }
    
    if list(filter(None, [thinking])):
        if thinking == True:
            data["thinking"] = True
        elif thinking == False:
            data["thinking"] = False
        elif thinking in ["low", "medium", "high"]:
            data["thinking"] = thinking

    if system:
        data["system"] = system

    if logprobs:
        data["logprobs"] = True
        if top_logprobs is not None:
            data["top_logprobs"] = top_logprobs
            
    if options:
        data['options'] = options

    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            for line in response:
                if line:
                    try:
                        yield json.loads(line.decode('utf-8'))
                    except ValueError:
                        pass
    except Exception as e:
        yield {"error": str(e)}
