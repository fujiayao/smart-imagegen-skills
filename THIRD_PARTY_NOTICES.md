# Third-party notices

## Scope

The root MIT LICENSE applies to this repository's original code and documentation. It does not relicense third-party prompt examples, images, account services, or the Codex runtime. User content is not included in the distribution.

## AwakeFantasy / relay-imagegen

- Repository: https://github.com/AwakeFantasy/relay-imagegen
- Pinned revision: `f3be3028b539dd5bb291e72c01baec9d6487934a`
- Redistributed file: `skills/imagegen-relay/third_party/relay-imagegen/scripts/image_client.py`
- SHA-256: `92549011fccead61d017beba07b22920775807f1a863477288a553c19e8da771`
- License: MIT, Copyright (c) 2026 AwakeFantasy.
- Original license is retained at `skills/imagegen-relay/third_party/relay-imagegen/LICENSE`.
- The file is copied without modification. The wrapper only imports its ImageClient and does not run the upstream GUI, skill instructions, configuration search, or provider failover.

## YouMind-OpenLab / gpt-image-2-prompts-search

- Repository: https://github.com/YouMind-OpenLab/gpt-image-2-prompts-search
- Pinned data revision: `d5d124e3662b5861f3e48d4c3c6de0bb4c7071ae`
- No LICENSE, COPYING, or NOTICE path was found in the complete Git tree for this revision during the 2026-10-18 release check. This is a version-specific observation, not a statement about all later versions.
- No prompt datasets or preview images are redistributed by this repository. The helper can fetch selected public JSON into an ignored local cache at runtime; user queries are searched locally.
- Public accessibility is not an open-source license. Confirm the upstream terms and the rights of individual examples/images for your intended use. This repository's MIT license grants no rights to that external content.
- Optional lookup failure does not block generation. Pinned revisions are not updated automatically.

## Other runtime dependencies

Python, the OpenAI Python SDK, Pillow, httpx, PyYAML, Codex, and any separately installed official image engine retain their own licenses and service terms. They are not bundled here. See the dependency distributions for their licenses. Image generation requires the relevant account permissions or a user-authorized API service and may incur charges.
