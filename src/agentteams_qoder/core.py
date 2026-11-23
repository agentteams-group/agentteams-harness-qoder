"""Load Core's bridge without shadowing the official SDK's `mcp` dependency."""
import importlib
import importlib.util
from pathlib import Path
import sys


def common(plugin, name):
    root = Path(plugin).resolve() / "mcp"
    package = "_agentteams_qoder_core"
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(package, root / "__init__.py",
                                                     submodule_search_locations=[str(root)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    elif Path(sys.modules[package].__file__).resolve() != root / "__init__.py":
        raise ValueError("Core bridge is already bound to another plugin directory")
    return importlib.import_module(package + ".common." + name)
