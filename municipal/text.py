"""Display formatting for values from the voter roll (stored UPPERCASE, often padded)."""
import re

_ORDINAL = re.compile(r'(\d)(St|Nd|Rd|Th)\b')
# Roman numerals II–XX as .title() renders them ('Ii', 'Iv', 'Xii', ...).
_ROMAN = re.compile(r'\b(Ii|Iii|Iv|Vi|Vii|Viii|Ix|Xi|Xii|Xiii|Xiv|Xv|Xvi|Xvii|Xviii|Xix|Xx)\b')


def title(s):
    """Title-case a roll value, keeping ordinals lowercase ('3Rd' -> '3rd')
    and Roman numerals uppercase ('Ihubok Ii' -> 'Ihubok II')."""
    text = (s or '').strip().title()
    text = _ORDINAL.sub(lambda m: m.group(1) + m.group(2).lower(), text)
    return _ROMAN.sub(lambda m: m.group(1).upper(), text)
