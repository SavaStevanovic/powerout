"""Serbian text helpers: Cyrillic/Latin, diacritics, and matching words across case endings.

The distributor writes in Cyrillic and in running prose ("делови улица Требињске, Космајске"), while a
person types "Trebinjska 12". Both sides are reduced to the same ASCII skeleton and compared word by word,
tolerating the endings that Serbian adds to a street name depending on the sentence.
"""

from __future__ import annotations

import os.path
import re
import unicodedata
from dataclasses import dataclass

_TO_LATIN: dict[str, str] = {}
for _pair in (
    "А:A Б:B В:V Г:G Д:D Ђ:Đ Е:E Ж:Ž З:Z И:I Ј:J К:K Л:L Љ:Lj М:M Н:N Њ:Nj О:O П:P Р:R С:S Т:T Ћ:Ć "
    "У:U Ф:F Х:H Ц:C Ч:Č Џ:Dž Ш:Š"
).split():
    _cyrillic, _latin = _pair.split(":")
    _TO_LATIN[_cyrillic] = _latin
    _TO_LATIN[_cyrillic.lower()] = _latin.lower()

# Words that are a part of an address but not of its name; a person may leave them out or the text may.
GENERIC = frozenset({"ulica", "ul", "bulevar", "bul", "trg", "put", "naselje", "nas", "selo", "mesto", "opstina", "grad"})
_SAINT = frozenset({"sv", "sveti", "svetog", "svetoga", "svetom", "svetomu", "svete", "svetoj", "svetu", "svetih"})
# What may follow the stem of a noun or adjective in some case or number (folded, so ć/č/š are plain).
_ENDINGS = frozenset({"", "a", "e", "i", "o", "u", "om", "em", "im", "ih", "oj", "ej", "og", "eg", "ma", "ima", "ama"})
# Words that lose an "a" before the ending: Pukovac, but "део Пуковца".
_FLEETING = (("ac", "c"), ("ak", "k"))

_WORD = re.compile(r"\w+")


def to_latin(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        latin = _TO_LATIN.get(ch, ch)
        if len(latin) == 2 and ch.isupper() and any(n.isupper() for n in text[max(i - 1, 0) : i] + text[i + 1 : i + 2]):
            latin = latin.upper()  # "ЉУБА" is "LJUBA", not "LjUBA"
        out.append(latin)
    return "".join(out)


def fold(text: str) -> str:
    """Lower-case ASCII skeleton: 'Ђорђе', 'Đorđe' and 'Djordje' all become 'djordje'."""
    decomposed = unicodedata.normalize("NFD", to_latin(text).lower().replace("đ", "dj"))
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@dataclass(frozen=True)
class Token:
    text: str  # as written
    key: str  # folded; "Св.", "Светог" and "sveti" are all "sv"
    start: int
    end: int
    initial: bool = False  # a first name abbreviated to its letter, "Д." in "Д.Ђорђевића", stands for any name with it


def tokenize(text: str) -> list[Token]:
    """Words of `text` with their positions in it (the text is expected to be Latin already)."""
    tokens = []
    for m in _WORD.finditer(text):
        key = fold(m.group())
        initial = len(key) == 1 and key.isalpha() and text[m.end() : m.end() + 1] == "."  # "и" (and) is not an initial
        tokens.append(Token(m.group(), "sv" if key in _SAINT else key, m.start(), m.end(), initial))
    return tokens


def query_keys(text: str) -> list[str]:
    """The words a person typed, ready for find_phrase. Generic words (ulica, bulevar...) are optional."""
    keys = [t.key for t in tokenize(to_latin(text))]
    return [k for k in keys if k not in GENERIC] or keys


def _forms(word: str):
    yield word
    for full, short in _FLEETING:
        if word.endswith(full) and len(word) > len(full) + 1:
            yield word[: -len(full)] + short  # Pukovac -> Pukovca, Leskovac -> Leskovcu


def _inflection(a: str, b: str) -> bool:
    stem = len(os.path.commonprefix([a, b]))
    return stem >= 3 and a[stem:] in _ENDINGS and b[stem:] in _ENDINGS


def matches(word: str, token: Token) -> bool:
    """Whether a word a person typed is the word in the text (which may be an initial of it)."""
    return same_word(word, token.key) or (token.initial and word.startswith(token.key))


def same_word(a: str, b: str) -> bool:
    """Whether two folded words are one word in two cases: trebinjska ~ trebinjske, milana ~ milanom.

    They must share a stem of at least three letters, and what follows it on either side must be an
    actual case ending. That accepts Sava ~ Save and Pukovac ~ Pukovca, and rejects Dušana ~ Dušanova,
    Rada ~ Radoja and Nikola ~ Nikolić. Numbers are never fuzzy.
    """
    if a == b:
        return True
    if any(c.isdigit() for c in a + b):
        return False
    return any(_inflection(x, y) for x in _forms(a) for y in _forms(b))


def find_phrase(query: list[str], tokens: list[Token], max_gap: int = 2) -> list[list[int]]:
    """Every place where the words of `query` follow each other in `tokens`, as lists of token indexes.

    Up to `max_gap` other words may sit between two of them ("Булевара Св. Пантелејмона").
    """
    if not query:
        return []
    found = []
    for first, token in enumerate(tokens):
        if not matches(query[0], token):
            continue
        indexes = [first]
        for word in query[1:]:
            window = range(indexes[-1] + 1, min(indexes[-1] + 2 + max_gap, len(tokens)))
            nxt = next((j for j in window if matches(word, tokens[j])), None)
            if nxt is None:
                break
            indexes.append(nxt)
        else:
            found.append(indexes)
    return found


def has_words(query: list[str], tokens: list[Token]) -> bool:
    """Whether every word of `query` occurs in `tokens`, in any order."""
    return bool(query) and all(any(matches(word, t) for t in tokens) for word in query)
