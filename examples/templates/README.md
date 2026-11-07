# Scrapping Plugin Cookiecutter Template

Use this template to generate a new scrapping-service plugin with the correct structure and conventions.

## Requirements

- Python 3.11+
- `cookiecutter` installed: `pip install cookiecutter`

## Usage

```bash
# From a directory where you want to create the plugin
cookiecutter gh:javiyt/scrapping-service examples/templates/scrapping-plugin-cookiecutter

# Or using the local template
cookiecutter examples/templates/scrapping-plugin-cookiecutter
```

The prompt will ask for:

- `project_name`: Human-readable project name
- `plugin_name`: Distribution name (PyPI, e.g. `scrapping-plugin-example`)
- `plugin_slug`: Importable module name (Python identifier, e.g. `scrapping_plugin_example`)
- `author_name`: Your name
- `author_email`: Your email
- `version`: Initial version (recommended: 0.1.0)
- `description`: One-line project description
- `entry_point_name`: Entry point name (e.g. `my-plugin`)

## What's generated

- `pyproject.toml`: Package manifest with correct entry point group
- `{{ cookiecutter.plugin_slug }}/plugin.py`: Plugin skeleton with `register()` stub
- `tests/test_contract.py`: Contract test template using `app.plugin_testing`
- `README.md`: Quick start, config, contract tests, API reference
- `LICENSE`: MIT

## Conventions

- Distribution name: `scrapping-plugin-<name>`
- Import module: underscored version (`scrapping-plugin-example` → `scrapping_plugin_example`)
- Entry point group: `scrapping_service.plugins`
- Config namespace: `SCRAPER_PLUGIN_<NAME>_*` (uppercase with dashes replaced by underscores)
- Plugin API version: must match `app.plugin_api.PLUGIN_API_VERSION` major

## Next steps

1. Implement your provider(s) in `plugin.py`
2. Run contract tests: `pytest tests/ -v`
3. Register the plugin in your `pyproject.toml`
4. Publish to PyPI or install via pip for local testing