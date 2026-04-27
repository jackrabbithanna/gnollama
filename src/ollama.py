import json
import urllib.request
import urllib.error
from typing import List, Dict, Any, Generator, Tuple, Optional

def fetch_models(host: str) -> List[str]:
    """
    Fetches the list of available models from the Ollama host.

    Args:
        host: The base URL of the Ollama host.

    Returns:
        A list of model names.
    """
    url = f"{host}/api/tags"
    try:
        with urllib.request.urlopen(url) as response:
            result = json.loads(response.read().decode('utf-8'))
            return [model['name'] for model in result.get('models', [])]
    except Exception as e:
        print(f"Failed to fetch models: {e}")
        return []

def fetch_model_details(host: str) -> List[Dict[str, Any]]:
    """
    Fetches the detailed list of available models from the Ollama host.

    Args:
        host: The base URL of the Ollama host.

    Returns:
        A list of dictionaries containing model details.
    """
    url = f"{host}/api/tags"
    try:
        with urllib.request.urlopen(url) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get('models', [])
    except Exception as e:
        print(f"Failed to fetch model details: {e}")
        return []

def show_model(host: str, name: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """
    Fetches detailed information about a specific model.

    Args:
        host: The base URL of the Ollama host.
        name: The name of the model.

    Returns:
        A tuple of (data_dict, error_string).
    """
    url = f"{host}/api/show"
    data = {
        "name": name,
        "verbose": False
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode('utf-8')), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode('utf-8')).get('error', str(e))
        except Exception:
            return None, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        print(f"Failed to show model: {e}")
        return None, str(e)

def get_version(host: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches the Ollama version.

    Args:
        host: The base URL of the Ollama host.

    Returns:
        A tuple of (version_string, error_string).
    """
    url = f"{host}/api/version"
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            result = json.loads(response.read().decode('utf-8'))
            return result.get('version', 'Unknown'), None
    except urllib.error.HTTPError as e:
        try:
            return None, json.loads(e.read().decode('utf-8')).get('error', str(e))
        except Exception:
            return None, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        print(f"Failed to fetch version: {e}")
        return None, str(e)

def delete_model(host: str, model_name: str) -> Tuple[bool, Optional[str]]:
    """
    Deletes a model from the Ollama host.

    Args:
        host: The base URL of the Ollama host.
        model_name: The name of the model to delete.

    Returns:
        A tuple of (success_boolean, error_string).
    """
    url = f"{host}/api/delete"
    data = {
        "model": model_name
    }
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'}, method='DELETE')
        with urllib.request.urlopen(req) as response:
            return True, None
    except urllib.error.HTTPError as e:
        try:
            return False, json.loads(e.read().decode('utf-8')).get('error', str(e))
        except Exception:
            return False, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        print(f"Failed to delete model: {e}")
        return False, str(e)

def pull(host: str, model: str, insecure: bool = False) -> Generator[Dict[str, Any], None, None]:
    """
    Generator that streams responses from the Ollama Pull API.

    Args:
        host: The base URL of the Ollama host.
        model: The name of the model to pull.
        insecure: Whether to allow insecure connections.

    Yields:
        Progress dictionaries from the Ollama API.
    """
    url = f"{host}/api/pull"
    
    data = {
        "model": model,
        "insecure": insecure,
        "stream": True
    }
    
    yield from _stream_response(url, data)

def generate(host: str, model: str, prompt: str, system: Optional[str] = None, 
             options: Optional[Dict[str, Any]] = None, thinking: Any = None, 
             logprobs: bool = False, top_logprobs: Optional[int] = None, 
             images: Optional[List[str]] = None) -> Generator[Dict[str, Any], None, None]:
    """
    Generator that streams responses from the Ollama Generate API.

    Args:
        host: The base URL of the Ollama host.
        model: The model name.
        prompt: The user prompt.
        system: Optional system prompt.
        options: Optional generation parameters.
        thinking: Optional thinking parameter.
        logprobs: Whether to return logprobs.
        top_logprobs: Number of top logprobs to return.
        images: Optional list of base64 encoded images.

    Yields:
        Response chunks from the Ollama API.
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

def chat(host: str, model: str, messages: List[Dict[str, Any]], 
         options: Optional[Dict[str, Any]] = None, thinking: Any = None, 
         logprobs: bool = False, top_logprobs: Optional[int] = None, 
         images: Optional[List[str]] = None) -> Generator[Dict[str, Any], None, None]:
    """
    Generator that streams responses from the Ollama Chat API.

    Args:
        host: The base URL of the Ollama host.
        model: The model name.
        messages: The chat history.
        options: Optional generation parameters.
        thinking: Optional thinking parameter.
        logprobs: Whether to return logprobs.
        top_logprobs: Number of top logprobs to return.
        images: Optional list of base64 encoded images.

    Yields:
        Response chunks from the Ollama API.
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

def _add_common_params(data: Dict[str, Any], options: Optional[Dict[str, Any]], 
                       thinking: Any, logprobs: bool, top_logprobs: Optional[int]) -> None:
    """Helper to add common parameters to the API request data."""
    if thinking is not None:
        if thinking is True:
            data["think"] = True
        elif thinking is False:
            data["think"] = False
        elif thinking in ["low", "medium", "high", "max"]:
            data["think"] = thinking

    if logprobs:
        data["logprobs"] = True
        if top_logprobs is not None:
            data["top_logprobs"] = top_logprobs
            
    if options:
        data['options'] = options

def _stream_response(url: str, data: Dict[str, Any]) -> Generator[Dict[str, Any], None, None]:
    """Internal helper to handle streaming JSON responses from Ollama."""
    try:
        req = urllib.request.Request(url, data=json.dumps(data).encode('utf-8'), headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req) as response:
            for line in response:
                if line:
                    try:
                        yield json.loads(line.decode('utf-8'))
                    except ValueError:
                        pass
    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode('utf-8')
            error_msg = json.loads(error_body).get('error', str(e))
            yield {"error": error_msg}
        except Exception:
            yield {"error": f"HTTP Error {e.code}: {e.reason}"}
    except Exception as e:
        yield {"error": str(e)}
