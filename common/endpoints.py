from typing import Dict, Optional


def get_endpoint_url(app_name: str, app_port: int, interface: str, param: Optional[Dict[str, str]] = None) -> str:
    if param is None:
        param = {}
    return f"http://{app_name}:{app_port}{interface.format(**param)}"
