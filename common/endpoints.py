from typing import Dict


def get_endpoint_url(app_name: str, app_port: int, interface: str, param: Dict[str, str]) -> str:
    return f"http://{app_name}:{app_port}{interface.format(**param)}"
