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

def fetch_model_details(host):
    """
    Fetches the detailed list of available models from the Ollama host.
    """
    url = f"{host}/api/tags"
    try:
        with urllib.request.urlopen(url) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get('models', [])
    except Exception as e:
        print(f"Failed to fetch model details: {e}")
        return []

def show_model(host, name):
    """
    Fetches detailed information about a specific model.
    """
    url = f"{host}/api/show"
    data = {
        "name": name,
        "verbose": False
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8'))
    except Exception as e:
        print(f"Failed to show model: {e}")
        return {}

def get_version(host):
    """
    Fetches the Ollama version.
    """
    url = f"{host}/api/version"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get('version', 'Unknown'), None
    except Exception as e:
        print(f"Failed to fetch version: {e}")
        return None, str(e)

def delete_model(host, model_name):
    """
    Deletes a model from the Ollama host.
    """
    url = f"{host}/api/delete"
    data = {
        "model": model_name
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='DELETE')
        with urllib.request.urlopen(req) as response:
            return True, None
    except Exception as e:
        print(f"Failed to delete model: {e}")
        return False, str(e)

def pull(host, model, insecure=False):
    """
    Generator that streams responses from the Ollama Pull API.
    """
    url = f"{host}/api/pull"
    
    data = {
        "model": model,
        "insecure": insecure,
        "stream": True
    }
    
    yield from _stream_response(url, data)

def generate(host, model, prompt, system=None, options=None, thinking=None, logprobs=False, top_logprobs=None, images=None):
    """
    Generator that streams responses from the Ollama Generate API.
    """
    url = f"{host}/api/generate"
    
    data = {
        "model": model,
        "prompt": prompt,
        "stream": True
    }
    
    
    if images:
        data["images"] = images

    _add_common_params(data, options, thinking, logprobs, top_logprobs)

    if system:
        data["system"] = system

    yield from _stream_response(url, data)

def chat(host, model, messages, options=None, thinking=None, logprobs=False, top_logprobs=None, images=None):
    """
    Generator that streams responses from the Ollama Chat API.
    """
    url = f"{host}/api/chat"
    
    data = {
        "model": model,
        "messages": messages,
        "stream": True
    }
    
    
    if images and data["messages"]:
        last_msg = data["messages"][-1]
        if last_msg.get("role") == "user":
            last_msg["images"] = images

    _add_common_params(data, options, thinking, logprobs, top_logprobs)

    yield from _stream_response(url, data)

def _add_common_params(data, options, thinking, logprobs, top_logprobs):
    if list(filter(None, [thinking])):
        if thinking == True:
            data["thinking"] = True
        elif thinking == False:
            data["thinking"] = False
        elif thinking in ["low", "medium", "high"]:
            data["thinking"] = thinking

    if logprobs:
        data["logprobs"] = True
        if top_logprobs is not None:
            data["top_logprobs"] = top_logprobs
            
    if options:
        data['options'] = options

def _stream_response(url, data):
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
