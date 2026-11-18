"""Artwork of a device in Chromium: what the browser draws, not only the markup."""

import io

from dcim.models import Cable, DeviceType, Interface
from PIL import Image

from .browser import browser_page
from .test_artwork_device_view import (
    FRONT_SVG,
    REAR_SVG,
    ArtworkTestCase,
    faces,
    links,
)


class ArtworkBrowserTests(ArtworkTestCase):
    """The artwork of a device whose eth1 has a cable."""

    def setUp(self):
        super().setUp()
        peer = self.make_device(
            "server1",
            DeviceType.objects.create(
                manufacturer=self.manufacturer, model="Server", slug="server"
            ),
        )
        Cable(
            a_terminations=[self.eth1],
            b_terminations=[
                Interface.objects.create(device=peer, name="eth0", type="1000base-t")
            ],
        ).save()
        self.activate()

    def test_the_outline_of_a_connector_without_paint_shows_in_the_browser(self):
        unpainted = (
            '<rect id="con-eth1" width="12" height="10" fill="none" stroke="none"/>'
        )
        self.store_face(
            "front.svg",
            # The green LED is next to the connector, so it goes.
            FRONT_SVG.replace(
                '<rect id="con-eth1" width="12" height="10" style="fill:url(#metal);stroke:#000"/>',
                unpainted,
            ).replace('<use xlink:href="#led" x="5" y="5"/>', ""),
        )
        with browser_page(self.tab()) as page:
            box = page.locator(
                f'[id="dv-o-d{self.device.pk}-front-con-eth1"]'
            ).bounding_box()
            margin = 8
            png = page.screenshot(
                clip={
                    "x": box["x"] - margin,
                    "y": box["y"] - margin,
                    "width": box["width"] + 2 * margin,
                    "height": box["height"] + 2 * margin,
                }
            )
        data = Image.open(io.BytesIO(png)).convert("RGB").tobytes()
        pixels = zip(data[0::3], data[1::3], data[2::3], strict=True)
        green = [p for p in pixels if p[1] - max(p[0], p[2]) > 60]
        self.assertGreater(len(green), 2 * (box["width"] + box["height"]))

    def test_a_transform_list_with_two_commas_moves_the_connector_in_the_browser(self):
        # The profile accepts two commas between transform functions; Chromium ignores such a list.
        self.store_face(
            "front.svg",
            FRONT_SVG.replace("translate(10 10)", "translate(10 10),,scale(1)"),
        )
        content = self.tab()
        ident = f"dv-o-d{self.device.pk}-front-con-eth1"
        group = links(faces(content)[0])[ident].getparent()
        self.assertEqual(group.get("transform"), "translate(10 10) scale(1)")
        with browser_page(content) as page:
            offset = page.evaluate(
                """ident => {
                    const list = document.getElementById(ident).closest("g").transform.baseVal;
                    return list.numberOfItems ? list.consolidate().matrix.e : null;
                }""",
                ident,
            )
        # The outline filter has its region in the same user space.
        self.assertEqual(offset, 10)

    def test_zoom_keeps_the_point_of_a_narrow_face_at_the_centre(self):
        # At zoom 1 the rear face is 450 px wide and 300 px high in a 500 px wide view.
        self.store_face(
            "rear.svg",
            REAR_SVG.replace('viewBox="0 0 440 44"', 'viewBox="0 0 150 100"'),
        )
        with browser_page(self.tab()) as page:
            point = """viewport => {
                const port = viewport.getBoundingClientRect();
                const face = viewport.querySelector(".dv-art-face").getBoundingClientRect();
                const x = port.left + viewport.clientLeft + viewport.clientWidth / 2;
                const y = port.top + viewport.clientTop + viewport.clientHeight / 2;
                return [(x - face.left) / face.width, (y - face.top) / face.height];
            }"""
            panel = page.locator(".dv-art-panel").nth(1)
            viewport = panel.locator(".dv-art-viewport")
            viewport.evaluate("""viewport => {
                    viewport.style.width = "500px";
                    viewport.style.setProperty("--dv-max-height", "300px");
                }""")
            before = viewport.evaluate(point)
            panel.locator('[data-dv-zoom="in"]').click()
            after = viewport.evaluate(point)
        self.assertAlmostEqual(before[0], 5 / 9, places=2)
        for start, end in zip(before, after, strict=True):
            self.assertAlmostEqual(start, end, places=2)

    def test_the_marks_of_a_translucent_connector_change_only_its_outline(self):
        self.store_face(
            "front.svg",
            FRONT_SVG.replace(
                '<rect id="con-eth1" width="12" height="10" '
                'style="fill:url(#metal);stroke:#000"/>',
                '<rect id="con-eth1" width="120" height="30" fill="#000" '
                'fill-opacity="0.5"/>',
            ),
        )
        Interface.objects.filter(pk=self.eth1.pk).update(enabled=False)
        ident = f"dv-o-d{self.device.pk}-front-con-eth1"
        with browser_page(self.tab()) as page:
            connector = page.locator(f'[id="{ident}"]')
            box = connector.bounding_box()
            # Five face units: well inside the outline band of one outline width.
            inset = 5 * box["width"] / 120

            def shots():
                inner = {
                    "x": box["x"] + inset,
                    "y": box["y"] + inset,
                    "width": box["width"] - 2 * inset,
                    "height": box["height"] - 2 * inset,
                }
                outer = {
                    "x": box["x"] - inset,
                    "y": box["y"] - inset,
                    "width": box["width"] + 2 * inset,
                    "height": box["height"] + 2 * inset,
                }
                return page.screenshot(clip=inner), page.screenshot(clip=outer)

            marked_inner, marked_outer = shots()
            connector.evaluate(
                "el => el.parentElement.querySelector('.dv-art-marks').remove()"
            )
            plain_inner, plain_outer = shots()
        self.assertEqual(marked_inner, plain_inner)
        self.assertNotEqual(marked_outer, plain_outer)
