"""
diaper_whitelist.py

Whitelist centralizada do catálogo Poupi — segmento fraldas.

Todo filtro de ingestão no data-core deve importar deste módulo.
Para futuras expansões de categoria, adicionar aqui e propagar via is_diaper().
"""

import re
import unicodedata

# ── Categorias aceitas ────────────────────────────────────────────────────────

DIAPER_CATEGORIES: frozenset[str] = frozenset(["baby", "fralda", "fraldas"])

# ── Marcas de fralda ──────────────────────────────────────────────────────────

DIAPER_BRANDS: frozenset[str] = frozenset([
    "pampers",
    "huggies",
    "mamypoko",
    "babysec",
    "personal",
    "cremer",
    "pompom",
    "turma da monica baby",
    "capricho baby",
    "sapeka",
])

# ── Palavras-chave de título aceitas ─────────────────────────────────────────

DIAPER_TITLE_KEYWORDS: frozenset[str] = frozenset([
    "fralda",
    "fraldas",
    "pants",
    "roupinha",
    "shortinho",
    "fraldinha",
])

# ── Palavras-chave banidas (excluem o produto mesmo que a categoria seja baby) ─

DIAPER_BANNED_KEYWORDS: frozenset[str] = frozenset([
    "formula",
    "leite",
    "composto lacteo",
    "nestogeno",
    "ninho",
    "aptamil",
    "milnutri",
    "mucilon",
    "lenco",
    "toalhinha",
    "umedecido",
    "pomada",
    "assadura",
    "hipoglos",
    "bepantol",
    "desitin",
    "mamadeira",
    "chupeta",
    "shampoo",
    "sabonete",
    "suplemento",
    "vitamina",
    "medicamento",
])

# ── Tamanhos canônicos ────────────────────────────────────────────────────────

DIAPER_SIZES: tuple[str, ...] = (
    "RN", "P", "M", "G", "XG", "XXG", "XXXG", "XXXXG", "EG", "XGG"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _normalize(text: str) -> str:
    """Lowercase + remove acentos + colapsa espaços."""
    nfd = unicodedata.normalize("NFD", text.lower())
    stripped = "".join(c for c in nfd if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", stripped).strip()


def is_diaper_category(category: str | None) -> bool:
    """Retorna True se a categoria do target pertence ao segmento fraldas."""
    if not category:
        return False
    return _normalize(category) in DIAPER_CATEGORIES


def title_matches_diaper(title: str) -> bool:
    """
    Retorna True se o título do produto contém pelo menos uma keyword de fralda
    e nenhuma keyword banida.
    """
    normalized = _normalize(title)
    has_diaper_kw = any(kw in normalized for kw in DIAPER_TITLE_KEYWORDS)
    has_banned_kw = any(kw in normalized for kw in DIAPER_BANNED_KEYWORDS)
    return has_diaper_kw and not has_banned_kw


def brand_is_diaper(brand: str | None) -> bool:
    """Retorna True se a marca pertence ao universo de fraldas."""
    if not brand:
        return False
    return _normalize(brand) in DIAPER_BRANDS
