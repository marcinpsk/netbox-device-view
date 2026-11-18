# Changelog

## [0.3.0](https://github.com/marcinpsk/netbox-device-view/compare/v0.5.0...v0.3.0) (2026-11-18)


### Features

* add device_tab_position config ([cc7405a](https://github.com/marcinpsk/netbox-device-view/commit/cc7405ac3ebd89dcf9b0253165c59db2756a7dd7))
* add SVG renderer with render_mode field and 58 new tests ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* add SVG renderer with render_mode field and 58 new tests ([eaa6a22](https://github.com/marcinpsk/netbox-device-view/commit/eaa6a2263c0c446a8bfd910911a102f45f9658fe))
* add SVG renderer, patch panel device tab support, improved list table ([a00f501](https://github.com/marcinpsk/netbox-device-view/commit/a00f501e23f7ebe5f7413ac166bac5fa902e99f1))
* add versioned rendered-layout API endpoint for API clients ([#66](https://github.com/marcinpsk/netbox-device-view/issues/66)) ([e007562](https://github.com/marcinpsk/netbox-device-view/commit/e007562b78f2049089136b52fbbb9430f8b92b6f))
* add YAML layout examples for 10 device types ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* add YAML layout examples for 10 device types ([9fb31fa](https://github.com/marcinpsk/netbox-device-view/commit/9fb31fac7804085ed6ff0f2d6ff6c7b78f12bf97))
* add YAML layout system with CSS Grid renderer ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* add YAML layout system with CSS Grid renderer ([5d68d77](https://github.com/marcinpsk/netbox-device-view/commit/5d68d770afa826cbdc33a695454b58babf914469))
* add YAML layout system with CSS Grid renderer ([b0e21ce](https://github.com/marcinpsk/netbox-device-view/commit/b0e21cea82cca55822c6eb21e8a930e5de44aed8))
* click navigates to port detail; tooltip adds Trace link ([5c1a5d8](https://github.com/marcinpsk/netbox-device-view/commit/5c1a5d8df9f6749c800960e6acb8914101291220))
* declare min_version and expand PyPI classifiers ([e9dbc63](https://github.com/marcinpsk/netbox-device-view/commit/e9dbc6373699a43c939b5949c459264105eebe52))
* highlight the currently-viewed device in a Virtual Chassis view ([b7061b3](https://github.com/marcinpsk/netbox-device-view/commit/b7061b334ec2167daa956ac1fbc8de49507ef447))
* implement Quick Search for Device Views list view ([2419c4d](https://github.com/marcinpsk/netbox-device-view/commit/2419c4def560bc04e297d62017f126ec0cd94327))
* import YAML and CSS layouts from Data Sources ([#68](https://github.com/marcinpsk/netbox-device-view/issues/68)) ([d911efb](https://github.com/marcinpsk/netbox-device-view/commit/d911efb1ee3bd2e2b2d16f2723373e07f59bb9c9))
* improve device view list table — linked device type, count, layout badges, render mode ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* improve device view list table — linked device type, count, layout badges, render mode; README migration note ([6082031](https://github.com/marcinpsk/netbox-device-view/commit/6082031c9c79f5438a4aff0eca1b2c8747a36b57))
* order virtual chassis panels by physical rack position respecting desc_units ([e36d323](https://github.com/marcinpsk/netbox-device-view/commit/e36d3230df3db8066b39f38c412bdad557f3e0a0))
* render console-server ports in device views ([#72](https://github.com/marcinpsk/netbox-device-view/issues/72)) ([6e604f8](https://github.com/marcinpsk/netbox-device-view/commit/6e604f8f534b1000b48b8d67136675feaf47370c))
* show live stylename preview on add/edit form ([cf9d615](https://github.com/marcinpsk/netbox-device-view/commit/cf9d6159da82a3bd845c949b23300bf9f194432d))
* **svg:** add partial connection indicator for SVG renderer ([#62](https://github.com/marcinpsk/netbox-device-view/issues/62)) ([5927254](https://github.com/marcinpsk/netbox-device-view/commit/59272545d9638e3a310478c8b7364c88000ba6ee)), closes [#61](https://github.com/marcinpsk/netbox-device-view/issues/61)
* switch tooltip to popover so Trace link is clickable ([be54797](https://github.com/marcinpsk/netbox-device-view/commit/be547977abf641c85b3cd0fe47da1280e48d95c0))


### Bug Fixes

* 17 numeric ports only ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* 17 numeric ports only ([483f907](https://github.com/marcinpsk/netbox-device-view/commit/483f90784f3ead6243b24c0126d9bed14ab3ad82))
* apply detail/trace link changes to ports.html ([a7fa915](https://github.com/marcinpsk/netbox-device-view/commit/a7fa915768ed610435bfeaf6b4987baeec31938d))
* apply the device view filter set to the list page and the API ([d3f7023](https://github.com/marcinpsk/netbox-device-view/commit/d3f70234849511a2d729f881919b74c32a93a764)), closes [#58](https://github.com/marcinpsk/netbox-device-view/issues/58)
* apply the device view filter set to the list page and the API ([#60](https://github.com/marcinpsk/netbox-device-view/issues/60)) ([38f9f0a](https://github.com/marcinpsk/netbox-device-view/commit/38f9f0a60e973d0d804a46ab6be0b874612414f8))
* apply the device view filter set to the list page and the API ([#74](https://github.com/marcinpsk/netbox-device-view/issues/74)) ([2a48382](https://github.com/marcinpsk/netbox-device-view/commit/2a48382ac4a9ad13ea33463c1b7ae3c99864c31e))
* build the catalog redirect from the route name ([#71](https://github.com/marcinpsk/netbox-device-view/issues/71)) ([bb788a9](https://github.com/marcinpsk/netbox-device-view/commit/bb788a96b863b5ba40372b0d262709cd96ae53e2))
* **ci:** pin ruff-action to v4.1.0 (no floating v4 tag exists yet) ([2c849a0](https://github.com/marcinpsk/netbox-device-view/commit/2c849a0c7e872f10ab1025c128e9e42df063618d))
* **ci:** repair COMPATIBILITY.md auto-insert; backfill missing rows ([42481ec](https://github.com/marcinpsk/netbox-device-view/commit/42481ec3d558609329e473bcd678c4861b5590aa))
* close code-review findings in the device view permissions ([abee63a](https://github.com/marcinpsk/netbox-device-view/commit/abee63aae4b9271c7c0771f7e0d34add86ed2537))
* connect a port on the page through its link peer, as the API does ([6a6d35c](https://github.com/marcinpsk/netbox-device-view/commit/6a6d35cd233198877d32031aa242e4bf78d0c37b))
* derive the connection state from the cable and peers that the user can view ([374537d](https://github.com/marcinpsk/netbox-device-view/commit/374537da21405208ba27ddaa0f5de3a659c4610f))
* detect horizontal col_span in css_to_yaml; add C9500-24Y4C YAML layout ([e361c46](https://github.com/marcinpsk/netbox-device-view/commit/e361c463fc4e00f64227919b6ec4df8997802079))
* equal 500ms show/hide delay on popovers to prevent overlap ([92cbaf5](https://github.com/marcinpsk/netbox-device-view/commit/92cbaf5e42210b05fa6ac88c4ec1f1f9d963280f))
* keep popover open when cursor moves into it ([b837b5c](https://github.com/marcinpsk/netbox-device-view/commit/b837b5c2ab150fee6099a6a1aba09f74acac6465))
* **lint:** resolve all ruff violations; exclude migrations from linting ([4c69e97](https://github.com/marcinpsk/netbox-device-view/commit/4c69e9720a9065393b7d47fafba7a38f99b40af3))
* prefetch the interface peers of every model in the rendered-layout API ([2e5f936](https://github.com/marcinpsk/netbox-device-view/commit/2e5f936bbafc963bc82d0fb18f4a65e2c93eefea))
* publish to PyPI on release_created, add workflow_dispatch, add v0.2.0 to COMPATIBILITY.md ([4a8dbe7](https://github.com/marcinpsk/netbox-device-view/commit/4a8dbe7b79456262b49311368e5ec3ddd44d786b))
* remove unused pytest import in test_css_to_yaml ([dff5c4f](https://github.com/marcinpsk/netbox-device-view/commit/dff5c4f311ce7247118fc1a7167b422ecfc3a637))
* sanitize non-standard whitespace in Grid Template Area field ([3af7cad](https://github.com/marcinpsk/netbox-device-view/commit/3af7cad0b3cbda4dec4c112b1bf4a4e80de26800))
* scope partial-connection indicator to correct chassis panel in virtual chassis ([4522dbe](https://github.com/marcinpsk/netbox-device-view/commit/4522dbebf77b99895b80993309829f1b2d8ad965))
* show only objects that the user can view in device views ([b539e84](https://github.com/marcinpsk/netbox-device-view/commit/b539e84289a70689f0e728f3fd30aef470823234)), closes [#56](https://github.com/marcinpsk/netbox-device-view/issues/56)
* show only objects that the user can view in device views ([#59](https://github.com/marcinpsk/netbox-device-view/issues/59)) ([2f5f869](https://github.com/marcinpsk/netbox-device-view/commit/2f5f869ad5cefd217d8af62fefde86ed8e56ae0b))
* show only objects that the user can view in device views ([#73](https://github.com/marcinpsk/netbox-device-view/issues/73)) ([b2869f2](https://github.com/marcinpsk/netbox-device-view/commit/b2869f229e188b2e5aa63c02be491d1dc4fb0310))
* show virtual chassis name in device view card header ([013c741](https://github.com/marcinpsk/netbox-device-view/commit/013c741df7a08fb79c4bb126cd4fd83de7b34926))
* suppress partial-connection indicator on patch panel ports ([a0a81c2](https://github.com/marcinpsk/netbox-device-view/commit/a0a81c235f7856d4caa7d5f49f2e97940b0aacf8))
* SVG JS — escapejs, nocolor pattern and pointer-events; patch panel SVG rendering on device tab ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* SVG JS — escapejs, nocolor pattern and pointer-events; patch panel SVG rendering on device tab, increase cell size; css_to_yaml command ([7cb9036](https://github.com/marcinpsk/netbox-device-view/commit/7cb9036f09f06168cbe1f7e59eae5b9ff633eb33))
* use window.Popover (NetBox global) and manually init CSS grid popovers ([f111350](https://github.com/marcinpsk/netbox-device-view/commit/f1113509bb8ba8817e112d7dd6cf53b13a14f2d1))
* validate yaml_layout on save and reject bad layout values ([#70](https://github.com/marcinpsk/netbox-device-view/issues/70)) ([01910e2](https://github.com/marcinpsk/netbox-device-view/commit/01910e2b5463bdb000460492f88258b5873dd35d))
* YAML examples — correct stylenames, interface prefixes, C2960X layout ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* YAML examples — correct stylenames, interface prefixes, C2960X layout, add row: field to pattern:all ([0267e78](https://github.com/marcinpsk/netbox-device-view/commit/0267e78cb37aba8dbaf28588c845a38e2b1fad23))
* YAML examples — correct stylenames, interface prefixes, C2960X layout, add row: field to pattern:all ([7dd30c3](https://github.com/marcinpsk/netbox-device-view/commit/7dd30c3f7636e6c29f51636635f4a2831fde9f0a))


### Performance Improvements

* read the cables and peers of the page components with their querysets ([20251d5](https://github.com/marcinpsk/netbox-device-view/commit/20251d5ddade472b7ef298003ac35ff4ab330f25))


### Documentation

* add C9200-48PXG layout examples with C9200-NM-4X variant ([8a8963b](https://github.com/marcinpsk/netbox-device-view/commit/8a8963b0da9e4fa22e2ee72549fb161949a61ea6))
* add YAML layout schema reference and update README ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* add YAML layout schema reference and update README ([5a01ef1](https://github.com/marcinpsk/netbox-device-view/commit/5a01ef13454806f4682733c99ff709db63804ca5))
* correct C9200-48PXG port types for ports 41-48 ([db119eb](https://github.com/marcinpsk/netbox-device-view/commit/db119eb49b182f844f983cc1af8a89e334810fa8))
* escape the pipe in the peer_labels table cell ([616fd5a](https://github.com/marcinpsk/netbox-device-view/commit/616fd5a1e43980d0c6919c1bae97c388fc63559e))
* redesign port-status SVG example — vertical card layout ([27864bd](https://github.com/marcinpsk/netbox-device-view/commit/27864bd0d59bc7e6e734e50fc00eb0e2d654a814))
* rewrite README and add COMPATIBILITY.md ([0171d33](https://github.com/marcinpsk/netbox-device-view/commit/0171d33ed23abf83a1345e4cc456b0c35a1ff78d))
* set up MkDocs Material site with port status SVG example ([#63](https://github.com/marcinpsk/netbox-device-view/issues/63)) ([154b7e6](https://github.com/marcinpsk/netbox-device-view/commit/154b7e6bf4bccb8ef82ad47adbc5eaf746626da4))
* thank marcinpsk in the 0.5.0 changelog ([8dfe286](https://github.com/marcinpsk/netbox-device-view/commit/8dfe286e8b3b39e61bdbf7436edf1df49b6247ba))


### Miscellaneous Chores

* release 0.3.0 ([19f0308](https://github.com/marcinpsk/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* trigger release-please for 0.3.0 ([0393c19](https://github.com/marcinpsk/netbox-device-view/commit/0393c19b33975c35f476e4ca8da416f1575d0af3))

## [0.5.0](https://github.com/peterbaumert/netbox-device-view/compare/v0.4.0...v0.5.0) (2026-11-17)


### Features

* declare min_version and expand PyPI classifiers ([e9dbc63](https://github.com/peterbaumert/netbox-device-view/commit/e9dbc6373699a43c939b5949c459264105eebe52))
* highlight the currently-viewed device in a Virtual Chassis view ([b7061b3](https://github.com/peterbaumert/netbox-device-view/commit/b7061b334ec2167daa956ac1fbc8de49507ef447))
* implement Quick Search for Device Views list view ([2419c4d](https://github.com/peterbaumert/netbox-device-view/commit/2419c4def560bc04e297d62017f126ec0cd94327))
* import YAML and CSS layouts from Data Sources ([#68](https://github.com/peterbaumert/netbox-device-view/issues/68)) ([d911efb](https://github.com/peterbaumert/netbox-device-view/commit/d911efb1ee3bd2e2b2d16f2723373e07f59bb9c9))
* render console-server ports in device views ([#72](https://github.com/peterbaumert/netbox-device-view/issues/72)) ([6e604f8](https://github.com/peterbaumert/netbox-device-view/commit/6e604f8f534b1000b48b8d67136675feaf47370c))


### Bug Fixes

* apply the device view filter set to the list page and the API ([#74](https://github.com/peterbaumert/netbox-device-view/issues/74)) ([2a48382](https://github.com/peterbaumert/netbox-device-view/commit/2a48382ac4a9ad13ea33463c1b7ae3c99864c31e))
* build the catalog redirect from the route name ([#71](https://github.com/peterbaumert/netbox-device-view/issues/71)) ([bb788a9](https://github.com/peterbaumert/netbox-device-view/commit/bb788a96b863b5ba40372b0d262709cd96ae53e2))
* **ci:** repair COMPATIBILITY.md auto-insert; backfill missing rows ([42481ec](https://github.com/peterbaumert/netbox-device-view/commit/42481ec3d558609329e473bcd678c4861b5590aa))
* sanitize non-standard whitespace in Grid Template Area field ([3af7cad](https://github.com/peterbaumert/netbox-device-view/commit/3af7cad0b3cbda4dec4c112b1bf4a4e80de26800))
* show only objects that the user can view in device views ([#73](https://github.com/peterbaumert/netbox-device-view/issues/73)) ([b2869f2](https://github.com/peterbaumert/netbox-device-view/commit/b2869f229e188b2e5aa63c02be491d1dc4fb0310))
* validate yaml_layout on save and reject bad layout values ([#70](https://github.com/peterbaumert/netbox-device-view/issues/70)) ([01910e2](https://github.com/peterbaumert/netbox-device-view/commit/01910e2b5463bdb000460492f88258b5873dd35d))

### Upgrade notes

* **Permissions:** the device page panel, the Device View tab and the rendered-layout API now show only objects the user can view: the device view, virtual chassis members, ports, modules, peers and cables. Users need the **view** permission on **Device View** (`netbox_device_view.deviceview`) to see the panel or the tab; add it to read-only roles before upgrading. The Device View tab now also applies object-level permission constraints on the device.
* **API filters:** `GET /api/plugins/device_view/device-view/` now applies its filters (before, it ignored them and returned every device view). `device_type` and `manufacturer` take slugs; use `device_type_id` and `manufacturer_id` for IDs. The list page no longer shows the Bulk Edit, Bulk Rename and Bulk Delete buttons, which never worked.
* New dependency `tinycss2>=1.5`. pip installs it with the plugin; NetBox does not ship it, so a custom image or an offline install needs it too.
* Saving a device view now parses its YAML layout, so the edit form, bulk import and REST API reject a layout that does not parse. A stored layout that does not parse shows a warning on the device page instead of a server error.
* Layout rendering changes for stored layouts: a variant that names a view the layout does not define now creates that view with its own face (rear instead of front), and merged views (`copy_from`, variants) keep every element that shares a key instead of only the last one.
* Console-server ports are now collected. In CSS mode, a layout without areas for their keys shows them as extra cells; add the keys to the layout. SVG mode does not draw them.

### Thanks

A big thank you to [@marcinpsk](https://github.com/marcinpsk), who contributed for the first time in this release: the Data Source import (#68), branching tests in CI (#69), layout validation (#70), the catalog redirect fix (#71), console-server ports (#72), object permissions (#73) and the device view filters (#74).

## [0.4.0](https://github.com/peterbaumert/netbox-device-view/compare/v0.3.0...v0.4.0) (2026-08-20)


### Features

* add versioned rendered-layout API endpoint for API clients ([#66](https://github.com/peterbaumert/netbox-device-view/issues/66)) ([e007562](https://github.com/peterbaumert/netbox-device-view/commit/e007562b78f2049089136b52fbbb9430f8b92b6f))


### Bug Fixes

* **ci:** pin ruff-action to v4.1.0 (no floating v4 tag exists yet) ([2c849a0](https://github.com/peterbaumert/netbox-device-view/commit/2c849a0c7e872f10ab1025c128e9e42df063618d))
* **lint:** resolve all ruff violations; exclude migrations from linting ([4c69e97](https://github.com/peterbaumert/netbox-device-view/commit/4c69e9720a9065393b7d47fafba7a38f99b40af3))

## [0.3.0](https://github.com/peterbaumert/netbox-device-view/compare/v0.3.0...v0.3.0) (2026-05-03)


### Features

* add SVG renderer with render_mode field and 58 new tests ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* add YAML layout examples for 10 device types ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* add YAML layout system with CSS Grid renderer ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* click navigates to port detail; tooltip adds Trace link ([5c1a5d8](https://github.com/peterbaumert/netbox-device-view/commit/5c1a5d8df9f6749c800960e6acb8914101291220))
* improve device view list table — linked device type, count, layout badges, render mode ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* order virtual chassis panels by physical rack position respecting desc_units ([e36d323](https://github.com/peterbaumert/netbox-device-view/commit/e36d3230df3db8066b39f38c412bdad557f3e0a0))
* **svg:** add partial connection indicator for SVG renderer ([#62](https://github.com/peterbaumert/netbox-device-view/issues/62)) ([5927254](https://github.com/peterbaumert/netbox-device-view/commit/59272545d9638e3a310478c8b7364c88000ba6ee)), closes [#61](https://github.com/peterbaumert/netbox-device-view/issues/61)
* switch tooltip to popover so Trace link is clickable ([be54797](https://github.com/peterbaumert/netbox-device-view/commit/be547977abf641c85b3cd0fe47da1280e48d95c0))


### Bug Fixes

* 17 numeric ports only ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* apply detail/trace link changes to ports.html ([a7fa915](https://github.com/peterbaumert/netbox-device-view/commit/a7fa915768ed610435bfeaf6b4987baeec31938d))
* detect horizontal col_span in css_to_yaml; add C9500-24Y4C YAML layout ([e361c46](https://github.com/peterbaumert/netbox-device-view/commit/e361c463fc4e00f64227919b6ec4df8997802079))
* equal 500ms show/hide delay on popovers to prevent overlap ([92cbaf5](https://github.com/peterbaumert/netbox-device-view/commit/92cbaf5e42210b05fa6ac88c4ec1f1f9d963280f))
* keep popover open when cursor moves into it ([b837b5c](https://github.com/peterbaumert/netbox-device-view/commit/b837b5c2ab150fee6099a6a1aba09f74acac6465))
* remove unused pytest import in test_css_to_yaml ([dff5c4f](https://github.com/peterbaumert/netbox-device-view/commit/dff5c4f311ce7247118fc1a7167b422ecfc3a637))
* scope partial-connection indicator to correct chassis panel in virtual chassis ([4522dbe](https://github.com/peterbaumert/netbox-device-view/commit/4522dbebf77b99895b80993309829f1b2d8ad965))
* show virtual chassis name in device view card header ([013c741](https://github.com/peterbaumert/netbox-device-view/commit/013c741df7a08fb79c4bb126cd4fd83de7b34926))
* suppress partial-connection indicator on patch panel ports ([a0a81c2](https://github.com/peterbaumert/netbox-device-view/commit/a0a81c235f7856d4caa7d5f49f2e97940b0aacf8))
* SVG JS — escapejs, nocolor pattern and pointer-events; patch panel SVG rendering on device tab ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* use window.Popover (NetBox global) and manually init CSS grid popovers ([f111350](https://github.com/peterbaumert/netbox-device-view/commit/f1113509bb8ba8817e112d7dd6cf53b13a14f2d1))
* YAML examples — correct stylenames, interface prefixes, C2960X layout ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))


### Documentation

* add C9200-48PXG layout examples with C9200-NM-4X variant ([8a8963b](https://github.com/peterbaumert/netbox-device-view/commit/8a8963b0da9e4fa22e2ee72549fb161949a61ea6))
* add YAML layout schema reference and update README ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* correct C9200-48PXG port types for ports 41-48 ([db119eb](https://github.com/peterbaumert/netbox-device-view/commit/db119eb49b182f844f983cc1af8a89e334810fa8))
* redesign port-status SVG example — vertical card layout ([27864bd](https://github.com/peterbaumert/netbox-device-view/commit/27864bd0d59bc7e6e734e50fc00eb0e2d654a814))
* set up MkDocs Material site with port status SVG example ([#63](https://github.com/peterbaumert/netbox-device-view/issues/63)) ([154b7e6](https://github.com/peterbaumert/netbox-device-view/commit/154b7e6bf4bccb8ef82ad47adbc5eaf746626da4))


### Miscellaneous Chores

* release 0.3.0 ([19f0308](https://github.com/peterbaumert/netbox-device-view/commit/19f03080152fc3e37ec6cfe011b00f7610f894a5))
* trigger release-please for 0.3.0 ([0393c19](https://github.com/peterbaumert/netbox-device-view/commit/0393c19b33975c35f476e4ca8da416f1575d0af3))

## [0.2.0](https://github.com/peterbaumert/netbox-device-view/compare/v0.1.15...v0.2.0) (2026-04-03)


### Features

* add device_tab_position config ([cc7405a](https://github.com/peterbaumert/netbox-device-view/commit/cc7405ac3ebd89dcf9b0253165c59db2756a7dd7))
* show live stylename preview on add/edit form ([cf9d615](https://github.com/peterbaumert/netbox-device-view/commit/cf9d6159da82a3bd845c949b23300bf9f194432d))


### Documentation

* rewrite README and add COMPATIBILITY.md ([0171d33](https://github.com/peterbaumert/netbox-device-view/commit/0171d33ed23abf83a1345e4cc456b0c35a1ff78d))
