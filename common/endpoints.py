from typing import Dict, Optional


def get_endpoint_url(app_name: str, app_port: int, interface: str, param: Optional[Dict[str, str]] = None) -> str:
    """Format the endpoint URL.

    Args:
        app_name (str): Container name (specifically hostname).
        app_port (int): Container port.
        interface (str): Router path including path params (using {var_name} format).
        param (Optional[Dict[str, str]], optional): Path params to be inserted in URL. Defaults to None.

    Returns:
        str: _description_
    """
    return f"http://{app_name}:{app_port}{interface.format(**(param or {}))}"
