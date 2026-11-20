from django.conf import settings
from netbox.plugins import PluginTemplateExtension

from .artwork.updates import device_update_pairs
from .forms import review_url
from .utils import device_view_context


class DevicePorts(PluginTemplateExtension):
    # NetBox reads only "models"; without it the extension renders on every object page.
    models = ("dcim.device",)

    def page(self):
        context = device_view_context(
            self.context["object"], self.context["request"].user
        )
        if context is None:
            return ""

        return self.render(
            "netbox_device_view/ports.html",
            extra_context=context,
        )

    def full_width_page(self):
        cfg = settings.PLUGINS_CONFIG["netbox_device_view"]
        if not cfg["show_on_device_tab"]:
            return ""
        if cfg.get("device_tab_position", "bottom") != "bottom":
            return ""
        return self.page()

    def alerts(self):
        cfg = settings.PLUGINS_CONFIG["netbox_device_view"]
        if not cfg["show_on_device_tab"]:
            return ""
        if cfg.get("device_tab_position", "bottom") != "top":
            return ""
        # Only render on the main device tab — all other tabs have a different URL path
        obj = self.context["object"]
        if self.context["request"].path != obj.get_absolute_url():
            return ""
        return self.page()

    def buttons(self):
        device = self.context["object"]
        pairs = device_update_pairs(self.context["request"].user, device)
        if not pairs:
            return ""
        return self.render(
            "netbox_device_view/buttons/artwork_update.html",
            extra_context={
                "url": review_url(pairs, return_url=device.get_absolute_url())
            },
        )


template_extensions = [DevicePorts]
