"""Text helpers more than one app needs.

Version-number rules (how many digits each part may have) stay in each app: they are part of that
app's spec, and they differ between apps.
"""

from __future__ import annotations

import regex


def graphemes(text: str) -> int:
    """Characters as people count them: an emoji, even 👨‍👩‍👧, is one.

    ★ Not len(): len() counts code points, and a family emoji is five. The Flutter apps count with
      Dart's characters package; both follow Unicode grapheme clusters, so limits agree.
    """
    return len(regex.findall(r"\X", text))
