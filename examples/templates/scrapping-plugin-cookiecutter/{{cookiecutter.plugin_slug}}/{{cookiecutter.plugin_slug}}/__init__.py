"""{{ cookiecutter.project_name }} initialization."""

from .plugin import {{ cookiecutter.entry_point_name.capitalize().replace('-', '_') }}

__all__ = ["{{ cookiecutter.entry_point_name.capitalize().replace('-', '_') }}"]