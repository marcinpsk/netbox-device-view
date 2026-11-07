from netbox.choices import ButtonColorChoices
from netbox.plugins import PluginMenuButton, PluginMenuItem

from .artwork.importing import PERMISSION

deviceview_buttons = [
    PluginMenuButton(
        link="plugins:netbox_device_view:deviceview_add",
        title="Add",
        icon_class="mdi mdi-plus-thick",
        color=ButtonColorChoices.GREEN,
    ),
    PluginMenuButton(
        link="plugins:netbox_device_view:deviceview_import",
        title="Import",
        icon_class="mdi mdi-upload",
    ),
]

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_device_view:deviceview_list",
        link_text="Device Views",
        buttons=deviceview_buttons,
    ),
    PluginMenuItem(
        link="plugins:netbox_device_view:visualdefinition_list",
        link_text="Visual Definitions",
        permissions=["netbox_device_view.view_visualdefinition"],
    ),
    PluginMenuItem(
        link="plugins:netbox_device_view:association_list",
        link_text="Artwork Associations",
        permissions=["netbox_device_view.view_association"],
    ),
    PluginMenuItem(
        link="plugins:netbox_device_view:bindingoverride_list",
        link_text="Binding Overrides",
        permissions=["netbox_device_view.view_bindingoverride"],
        buttons=[
            PluginMenuButton(
                link="plugins:netbox_device_view:bindingoverride_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
                color=ButtonColorChoices.GREEN,
                permissions=["netbox_device_view.add_bindingoverride"],
            )
        ],
    ),
    PluginMenuItem(
        link="plugins:netbox_device_view:definitionrevision_list",
        link_text="Artwork Revisions",
        permissions=["netbox_device_view.view_definitionrevision"],
        buttons=[
            PluginMenuButton(
                link="plugins:netbox_device_view:definitionrevision_import",
                title="Import bundle",
                icon_class="mdi mdi-upload",
                permissions=[PERMISSION],
            ),
            PluginMenuButton(
                link="plugins:netbox_device_view:definitionrevision_source_import",
                title="Import from Data Source",
                icon_class="mdi mdi-database-import",
                permissions=[PERMISSION],
            ),
        ],
    ),
)
