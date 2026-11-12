"""Wiro AI nodes for ComfyUI: https://github.com/wiroai/Wiro-ComfyUI"""

WEB_DIRECTORY = "./web"

if __package__:
    from .wiro_comfyui import catalog as _catalog
    from .wiro_comfyui.nodes import NODE_CLASS_MAPPINGS, NODE_DISPLAY_NAME_MAPPINGS
    from .wiro_comfyui.server_hooks import register as _register_server_hooks

    _catalog.start()
    _register_server_hooks()
else:  # imported as a plain file, for example by pytest collecting this folder
    NODE_CLASS_MAPPINGS = {}
    NODE_DISPLAY_NAME_MAPPINGS = {}

__all__ = ["NODE_CLASS_MAPPINGS", "NODE_DISPLAY_NAME_MAPPINGS", "WEB_DIRECTORY"]
