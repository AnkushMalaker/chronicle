from pathlib import Path

from advanced_omi_backend.services.memory.vault_media import (
    promote_image_bytes,
    sniff_image_type,
)
from advanced_omi_backend.services.person_photos import (
    embed_photo,
    has_photo_embed,
    match_person,
)

PERSON_NOTE = """\
---
categories:
  - "[[People]]"
created: 2026-07-22
---
## About
- Friend from college.
"""


def person(identifier: str, name: str, hidden: bool = False):
    return {"id": identifier, "name": name, "isHidden": hidden}


def test_match_person_prefers_exact_name():
    people = [person("a", "Ankush Malaker"), person("b", "Ankush")]
    assert match_person("ankush", people)["id"] == "b"


def test_match_person_accepts_unique_first_name_prefix():
    people = [person("a", "Ankush Malaker"), person("b", "Daksh")]
    assert match_person("Ankush", people)["id"] == "a"


def test_match_person_rejects_ambiguous_and_hidden():
    ambiguous = [person("a", "Ankush Malaker"), person("b", "Ankush Kumar")]
    assert match_person("Ankush", ambiguous) is None
    assert match_person("Ankush", [person("a", "Ankush", hidden=True)]) is None
    assert match_person("Ankush", [person("a", "")]) is None


def test_embed_photo_inserts_below_frontmatter():
    result = embed_photo(PERSON_NOTE, "_media/abc.jpg")
    assert has_photo_embed(result)
    frontmatter_end = result.index("---\n", 4)
    assert result.index("![[../_media/abc.jpg|200]]") > frontmatter_end
    assert result.index("![[../_media/abc.jpg|200]]") < result.index("## About")


def test_embed_photo_without_frontmatter_prepends():
    result = embed_photo("## About\n", "_media/abc.jpg")
    assert result.startswith("![[../_media/abc.jpg|200]]\n")


def test_sniff_image_type_detects_common_encodings():
    assert sniff_image_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"
    assert sniff_image_type(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_image_type(b"RIFF\x00\x00\x00\x00WEBPrest") == "image/webp"
    assert sniff_image_type(b"not an image") is None


def test_promote_image_bytes_is_content_addressed(tmp_path: Path):
    data = b"\xff\xd8\xff\xe0fake-jpeg"
    first, digest = promote_image_bytes(data, "image/jpeg", tmp_path)
    second, _ = promote_image_bytes(data, "image/jpeg", tmp_path)
    assert first == second == f"_media/{digest}.jpg"
    assert (tmp_path / first).read_bytes() == data
