# Check a Layout Library

The `device_view_check` management command checks the files of a layout library, for example a Git repository that you use as a Data Source. It uses the validation code of the imports, so a file that passes the check passes the import validation of the same plugin version. Run it before you commit, or in the CI of the library.

The command reads only files. It needs a NetBox installation with the plugin, but no database connection.

```bash
python manage.py device_view_check /path/to/library
python manage.py device_view_check /path/to/library --device-types /path/to/devicetype-library
```

## What the command checks

The library directory has this structure:

```text
library/
├── device-types/<Manufacturer>/<name>.yaml    # layouts
└── artwork/<Manufacturer>/<name>/             # artwork definitions
    ├── manifest.yaml
    └── <face>.svg
```

- **Layouts:** each `.yaml`, `.yml` and `.css` file below `device-types/` must pass the [checks of the Data Source import](import-from-data-source.md#what-the-import-checks) for its content and size. The command does not match the files to device types.
- **Artwork:** all files below `artwork/` must pass the validation of an [artwork import from a Data Source](artwork-bundles.md#import-from-a-data-source) with the prefix `artwork/`, with its path and size limits. Every file must be in a definition directory.
- **Names (with `--device-types`):** each binding and each bay of an artwork definition must name a component or a module bay of the [devicetype-library](https://github.com/netbox-community/devicetype-library) type at the same path. The type of `artwork/<Manufacturer>/<name>/` is `device-types/<Manufacturer>/<name>.yaml` for a device definition, and `module-types/<Manufacturer>/<name>.yaml` for a module definition. The artwork import accepts any name, so without this check a wrong name shows on the device page as no component found.

The command writes each problem as `path:line: message`, or `path: message` when no line applies, and exits with an error. Paths are relative to the library directory. A directory with no `device-types/` and no `artwork/` directory is an error. When the command finds no problem, it writes the number of layouts and artwork definitions that it checked.

## Run the check with Docker

To run the check without a NetBox installation, use the NetBox image. Set `PLUGIN` to the plugin version that you check against, as a pip requirement, for example `netbox-device-view==<version>` or `netbox-device-view @ https://github.com/<owner>/netbox-device-view/archive/<commit>.tar.gz`. The container needs a secret key, but no database or Redis server:

```bash
docker run --rm -v "$PWD:/library:ro" -e PLUGIN \
  -e SECRET_KEY=check-only-not-a-secret-0123456789abcdefghijklmnopqrstuv \
  netboxcommunity/netbox:v4.7.4 sh -c '
    uv pip install --python /opt/netbox/venv/bin/python "$PLUGIN" &&
    echo "PLUGINS = [\"netbox_device_view\"]" > /etc/netbox/config/plugins.py &&
    /opt/netbox/venv/bin/python /opt/netbox/netbox/manage.py device_view_check /library'
```

The [netbox-device-view-library](https://github.com/marcinpsk/netbox-device-view-library) repository runs the check in its CI this way.
