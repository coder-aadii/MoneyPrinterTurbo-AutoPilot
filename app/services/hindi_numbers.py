"""Conservative Hindi spoken-text normalization for numeric expressions."""

from __future__ import annotations

import re


_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")
_SMALL = (
    "शून्य", "एक", "दो", "तीन", "चार", "पाँच", "छह", "सात", "आठ", "नौ",
    "दस", "ग्यारह", "बारह", "तेरह", "चौदह", "पंद्रह", "सोलह", "सत्रह",
    "अठारह", "उन्नीस",
)
_TENS = (
    "", "", "बीस", "तीस", "चालीस", "पचास", "साठ", "सत्तर", "अस्सी", "नब्बे",
)
_COMPOUND = {
    21: "इक्कीस", 22: "बाईस", 23: "तेईस", 24: "चौबीस", 25: "पच्चीस",
    26: "छब्बीस", 27: "सत्ताईस", 28: "अट्ठाईस", 29: "उनतीस",
    31: "इकतीस", 32: "बत्तीस", 33: "तैंतीस", 34: "चौंतीस", 35: "पैंतीस",
    36: "छत्तीस", 37: "सैंतीस", 38: "अड़तीस", 39: "उनतालीस",
    41: "इकतालीस", 42: "बयालीस", 43: "तैंतालीस", 44: "चवालीस", 45: "पैंतालीस",
    46: "छियालीस", 47: "सैंतालीस", 48: "अड़तालीस", 49: "उनचास",
    51: "इक्यावन", 52: "बावन", 53: "तिरपन", 54: "चौवन", 55: "पचपन",
    56: "छप्पन", 57: "सत्तावन", 58: "अट्ठावन", 59: "उनसठ",
    61: "इकसठ", 62: "बासठ", 63: "तिरसठ", 64: "चौंसठ", 65: "पैंसठ",
    66: "छियासठ", 67: "सड़सठ", 68: "अड़सठ", 69: "उनहत्तर",
    71: "इकहत्तर", 72: "बहत्तर", 73: "तिहत्तर", 74: "चौहत्तर", 75: "पचहत्तर",
    76: "छिहत्तर", 77: "सतहत्तर", 78: "अठहत्तर", 79: "उनासी",
    81: "इक्यासी", 82: "बयासी", 83: "तिरासी", 84: "चौरासी", 85: "पचासी",
    86: "छियासी", 87: "सत्तासी", 88: "अठासी", 89: "नवासी",
    91: "इक्यानवे", 92: "बानवे", 93: "तिरानवे", 94: "चौरानवे", 95: "पचानवे",
    96: "छियानवे", 97: "सत्तानवे", 98: "अट्ठानवे", 99: "निन्यानवे",
}
_MONTHS = {
    "january": "जनवरी", "february": "फरवरी", "march": "मार्च",
    "april": "अप्रैल", "may": "मई", "june": "जून", "july": "जुलाई",
    "august": "अगस्त", "september": "सितंबर", "october": "अक्टूबर",
    "november": "नवंबर", "december": "दिसंबर",
}
_UNITS = {
    "km/h": "किलोमीटर प्रति घंटा", "km": "किलोमीटर", "kilometers": "किलोमीटर",
    "kilometer": "किलोमीटर", "kms": "किलोमीटर", "cm": "सेंटीमीटर",
    "centimeters": "सेंटीमीटर", "centimeter": "सेंटीमीटर", "mm": "मिलीमीटर",
    "m": "मीटर", "meters": "मीटर", "meter": "मीटर", "kg": "किलोग्राम",
    "kilograms": "किलोग्राम", "kilogram": "किलोग्राम", "g": "ग्राम",
    "grams": "ग्राम", "gram": "ग्राम", "mg": "मिलीग्राम", "mph": "मील प्रति घंटा",
    "°c": "डिग्री सेल्सियस", "celsius": "डिग्री सेल्सियस",
    "°f": "डिग्री फ़ारेनहाइट", "fahrenheit": "डिग्री फ़ारेनहाइट",
    "hours": "घंटे", "hour": "घंटे", "hrs": "घंटे", "hr": "घंटे",
    "minutes": "मिनट", "minute": "मिनट", "mins": "मिनट", "min": "मिनट",
    "seconds": "सेकंड", "second": "सेकंड", "secs": "सेकंड", "sec": "सेकंड",
    "days": "दिन", "day": "दिन", "years": "साल", "year": "साल",
    "weeks": "हफ़्ते", "week": "हफ़्ते", "months": "महीने", "month": "महीने",
}
_UNIT_PATTERN = "|".join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True))
_NUMBER = r"(?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d+)?"
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_DATE_DMY = re.compile(r"(?<!\d)(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?!\d)")
_DATE_MONTH = re.compile(
    r"(?<!\w)(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})(?!\d)", re.IGNORECASE
)
_UNIT_VALUE = re.compile(rf"(?<![\w.])({_NUMBER})\s*({_UNIT_PATTERN})\b", re.IGNORECASE)
_PERCENT_VALUE = re.compile(rf"(?<![\w.])({_NUMBER})\s*(%|percent(?:age)?\b)", re.IGNORECASE)
_HINDI_PERCENT = re.compile(rf"(?<![\w.])({_NUMBER})\s+प्रतिशत")
_NUMBER_TOKEN = re.compile(rf"(?<![\w.-])({_NUMBER})(?![\w.-])")
_MONTH_NAMES = tuple(_MONTHS)


def _under_hundred(value: int) -> str:
    if value < 20:
        return _SMALL[value]
    if value % 10 == 0:
        return _TENS[value // 10]
    return _COMPOUND.get(value, _TENS[value // 10] + " " + _SMALL[value % 10]).strip()


def _under_thousand(value: int) -> str:
    parts = []
    if value >= 100:
        hundreds, value = divmod(value, 100)
        parts.extend((_under_hundred(hundreds), "सौ"))
    if value:
        parts.append(_under_hundred(value))
    return " ".join(parts) or _SMALL[0]


def cardinal_hindi(value: int) -> str:
    """Spell a nonnegative integer using common Hindi cardinal forms."""
    if value < 0:
        return "माइनस " + cardinal_hindi(-value)
    if value < 1000:
        return _under_thousand(value)
    groups = ((10_000_000, "करोड़"), (100_000, "लाख"), (1000, "हज़ार"))
    parts = []
    remainder = value
    for divisor, label in groups:
        count, remainder = divmod(remainder, divisor)
        if count:
            parts.extend((cardinal_hindi(count), label))
    if remainder:
        parts.append(_under_thousand(remainder))
    return " ".join(parts)


def _number_words(raw: str) -> str:
    canonical = raw.translate(_DIGITS).replace(",", "")
    if "." in canonical:
        whole, fractional = canonical.split(".", 1)
        fraction_words = " ".join(_SMALL[int(digit)] for digit in fractional)
        return f"{cardinal_hindi(int(whole or 0))} दशमलव {fraction_words}"
    return cardinal_hindi(int(canonical))


def _date_iso(match: re.Match) -> str:
    year, month, day = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return match.group(0)
    month_name = tuple(_MONTHS.values())[month - 1]
    return f"{cardinal_hindi(day)} {month_name} {cardinal_hindi(year)}"


def _date_dmy(match: re.Match) -> str:
    day, month, year = (int(part) for part in match.groups())
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        return match.group(0)
    month_name = tuple(_MONTHS.values())[month - 1]
    return f"{cardinal_hindi(day)} {month_name} {cardinal_hindi(year)}"


def _date_named_month(match: re.Match) -> str:
    day, month, year = match.groups()
    return f"{cardinal_hindi(int(day))} {_MONTHS[month.casefold()]} {cardinal_hindi(int(year))}"


def normalize_hindi_numbers(text: str, language: str = "hi") -> str:
    """Expand supported numerals into words before Hindi TTS/alignment.

    Non-Hindi narration is returned unchanged. Dates are interpreted as ISO
    year-month-day or, for slash-separated forms, Hindi day/month/year.
    Unknown numeric syntax remains numeric so strict alignment can fail visibly.
    """
    if not isinstance(text, str) or not text:
        return text or ""
    language_key = str(language or "").strip().casefold()
    if language_key and not (language_key == "hi" or language_key.startswith("hindi")
                             or language_key.startswith("hi-")):
        return text
    text = text.translate(_DIGITS)
    text = _DATE_ISO.sub(_date_iso, text)
    text = _DATE_DMY.sub(_date_dmy, text)
    text = _DATE_MONTH.sub(_date_named_month, text)

    def unit_value(match: re.Match) -> str:
        return f"{_number_words(match.group(1))} {_UNITS[match.group(2).casefold()]}"

    text = _UNIT_VALUE.sub(unit_value, text)
    text = _PERCENT_VALUE.sub(lambda match: f"{_number_words(match.group(1))} प्रतिशत", text)
    text = _HINDI_PERCENT.sub(lambda match: f"{_number_words(match.group(1))} प्रतिशत", text)
    return _NUMBER_TOKEN.sub(lambda match: _number_words(match.group(1)), text)
