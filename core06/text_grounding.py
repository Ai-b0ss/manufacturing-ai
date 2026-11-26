"""Shared stdlib-only engineering identifier, OCR-fuzzy, and citation grounding helpers.

Used by both standalone (ai_extension.py / app.py) and v0.6 (core06/*).
Homoglyph and OCR-fuzzy matches are strictly candidate flags requiring human
verification, NEVER proof of engineering identity.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

STOP_WORDS = frozenset({
    "что", "как", "если", "при", "или", "это", "где", "чего", "для", "без",
    "какой", "какая", "какое", "какие", "какова", "каковы", "каков",
    "почему", "есть", "под", "про", "этот", "этой", "этом", "этих", "мне",
    "нужно", "можно", "должно", "таким", "согласно", "расскажи",
    "документе", "документах", "указан", "указана", "указано", "указаны",
    "the", "and", "for", "with", "from", "what", "which", "that", "this",
})

IDENTIFIER_PATTERN = re.compile(
    r"(?<!\w)(?=[A-Za-zА-Яа-яЁё0-9.\-]*\d)"
    r"(?:[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё0-9.\-]{2,}[A-Za-zА-Яа-яЁё0-9]|"
    r"(?=\d{1,4}[A-Za-zА-Яа-яЁё])\d[A-Za-zА-Яа-яЁё0-9.\-]{3,}[A-Za-zА-Яа-яЁё0-9])(?!\w)",
    flags=re.UNICODE,
)

# Latin lookalikes mapped to Cyrillic uppercase ONLY as a secondary candidate key.
IDENTIFIER_CONFUSABLE = str.maketrans({
    "A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К",
    "M": "М", "O": "О", "P": "Р", "T": "Т", "X": "Х", "Y": "У",
})

# Cyrillic letters frequently confused with digits by Russian OCR (WinRT/Tesseract).
# Applied ONLY to chunks whose extraction_method == 'ocr' with lexical corroboration.
_OCR_DIGIT_CONFUSABLE = str.maketrans({
    "О": "0",
    "З": "3",
})

# Exclusionary/contrastive phrases indicating a cross-reference to a DIFFERENT
# item rather than a specification of the referenced identifier.
_NEGATIVE_CONTEXT_PATTERN = re.compile(
    r"(?:"
    r"не\s+смешивать\s+с|"
    r"не\s+путать\s+с|"
    r"не\s+относится\s+к|"
    r"не\s+применять\s+(?:для|к)|"
    r"не\s+предназнач(?:ен|ена|ено|ены)\s+для|"
    r"не\s+совместим(?:а|о|ы)?\s+с|"
    r"в\s+отличие\s+от|"
    r"отличается\s+от|"
    r"взамен|"
    r"вместо|"
    r"за\s+исключением|"
    r"do\s+not\s+(?:mix|confuse)\s+with|"
    r"not\s+(?:applicable\s+to|intended\s+for|for\s+use\s+with)|"
    r"instead\s+of|"
    r"unlike"
    r")\b",
    flags=re.IGNORECASE | re.UNICODE,
)

_COMPARISON_QUERY_PATTERN = re.compile(
    r"\b(?:сравни\w*|отлич\w*|разниц\w*|не\s+смешива\w*|путать|compare|differ\w*)\b",
    flags=re.IGNORECASE | re.UNICODE,
)

_ABBREV_BEFORE_DOT_RE = re.compile(
    r"(?<!\w)(?:изд|дет|поз|пп|п|см|рис|табл|стр|гл|разд|др|пр|напр|ст|ч|"
    r"т\.е|т\.д|т\.п|fig|no|pos|rev|ref|sec)$",
    flags=re.IGNORECASE | re.UNICODE,
)
_COMPOUND_TOKEN_PATTERN = re.compile(r"(?u)[0-9A-Za-zА-Яа-яЁё]+(?:[.\-][0-9A-Za-zА-Яа-яЁё]+)*")
_CITATION_TAG_PATTERN = re.compile(r"\[(\d{1,3})\]")


def query_tokens(text: str) -> list[str]:
    """Keep significant words and two-digit+ numbers; deduplicate in order."""
    raw = re.findall(r"[\w]+", text.casefold().replace("ё", "е"), flags=re.UNICODE)
    out: list[str] = []
    for token in raw:
        if token in STOP_WORDS or not (len(token) > 2 or (token.isdecimal() and len(token) >= 2)):
            continue
        if token not in out:
            out.append(token)
        if len(out) == 16:
            break
    return out


def extract_identifiers(text: str) -> list[str]:
    return IDENTIFIER_PATTERN.findall(text)


def identifier_key(value: str) -> str:
    """Secondary candidate key (Latin->Cyrillic lookalikes), NEVER identity proof."""
    return re.sub(r"[.\-\s]", "", value.upper().translate(IDENTIFIER_CONFUSABLE))


def ocr_identifier_key(value: str) -> str:
    """OCR candidate key folding Cyrillic О->0 and З->3 on top of identifier_key."""
    return identifier_key(value).translate(_OCR_DIGIT_CONFUSABLE)


def contains_number(text: str, number: str) -> bool:
    """Boundary-aware numeric match: 25 does not match 125, 250, or 025."""
    return re.search(r"(?<!\d)" + re.escape(number) + r"(?!\d)", text) is not None


def strip_identifiers(text: str) -> str:
    return IDENTIFIER_PATTERN.sub(" ", text)


def standalone_numbers(text: str) -> list[str]:
    """Numbers in the query that are NOT embedded inside an engineering identifier."""
    stripped = strip_identifiers(text)
    return [t for t in query_tokens(stripped) if t.isdecimal()]


def non_identifier_tokens(text: str) -> list[str]:
    """Non-numeric, non-stopword context tokens outside engineering identifiers."""
    stripped = strip_identifiers(text)
    return [t for t in query_tokens(stripped) if not t.isdecimal()]


def _last_clause(window: str) -> str:
    """Return the trailing sentence/clause fragment before an identifier.

    Preserves dots inside dotted ESKD/GOST identifiers (e.g. АБВГ.712345.004-02)
    and common technical abbreviations (изд., дет., поз., п., рис., см.) while
    splitting on true sentence boundaries and hard delimiters (!?;:\\n).
    """
    cut = 0
    n = len(window)
    for i, ch in enumerate(window):
        if ch in "!?;:\n":
            cut = i + 1
        elif ch == ".":
            if i > 0 and i + 1 < n and window[i - 1].isalnum() and window[i + 1].isalnum():
                continue
            if _ABBREV_BEFORE_DOT_RE.search(window[:i]):
                continue
            cut = i + 1
    return window[cut:]


def is_negative_mention(text: str, start: int) -> bool:
    """Return True if an identifier at offset `start` appears in an exclusion clause."""
    window = text[max(0, start - 96):start]
    clause = _last_clause(window)
    return _NEGATIVE_CONTEXT_PATTERN.search(clause) is not None


def query_requests_comparison(question: str) -> bool:
    return _COMPARISON_QUERY_PATTERN.search(question) is not None


def _count_context_hits(tokens: list[str], heading: str, body: str) -> int:
    hits = 0
    for token in tokens:
        stem = token[:min(8, max(4, len(token) - 2))] if len(token) > 5 else token
        exact = token in heading or token in body
        fuzzy = len(stem) > 3 and (stem in heading or stem in body)
        if exact or fuzzy:
            hits += 1
    return hits


@dataclass(frozen=True)
class IdentifierEvaluation:
    matched_keys: frozenset[str]
    negative_only_keys: frozenset[str]
    heading_keys: frozenset[str]
    match_type: str | None
    sibling_variant_conflict: bool
    corroborating_hits: int


def evaluate_identifier_match(
    question: str,
    title: str,
    designation: str,
    body: str,
    extraction_method: str = "text",
    *,
    allow_negative: bool | None = None,
) -> IdentifierEvaluation:
    """Evaluate how a document/chunk matches engineering identifiers in `question`.

    - Positive matches in title/designation/body are preferred over exclusionary
      references ("не смешивать с Н-125").
    - OCR-fuzzy matches (О/0, З/3) are ONLY permitted when extraction_method=='ocr'
      AND at least one non-identifier context token from the question corroborates
      the chunk (prevents bare material-grade false positives like 12Х18Н1ОТ).
    """
    q_ids = extract_identifiers(question)
    heading_raw = f"{title or ''} {designation or ''}".strip()
    heading_norm = heading_raw.casefold().replace("ё", "е")
    body_norm = (body or "").casefold().replace("ё", "е")
    context_tokens = non_identifier_tokens(question)
    corroborating = _count_context_hits(context_tokens, heading_norm, body_norm)

    if not q_ids:
        return IdentifierEvaluation(
            matched_keys=frozenset(),
            negative_only_keys=frozenset(),
            heading_keys=frozenset(),
            match_type=None,
            sibling_variant_conflict=False,
            corroborating_hits=corroborating,
        )

    if allow_negative is None:
        allow_negative = query_requests_comparison(question)

    raw_by_key: dict[str, set[str]] = {}
    ocr_to_qkeys: dict[str, set[str]] = {}
    for code in q_ids:
        k = identifier_key(code)
        raw_by_key.setdefault(k, set()).add(code.casefold())
        ocr_to_qkeys.setdefault(ocr_identifier_key(code), set()).add(k)

    q_keys = set(raw_by_key)
    is_ocr = extraction_method == "ocr"
    allow_ocr_fuzzy = is_ocr and corroborating >= 1

    # Track best match quality per query key for positive vs negative mentions.
    # Rank: exact (3) > homoglyph_candidate (2) > ocr_fuzzy_candidate (1).
    pos_quality: dict[str, int] = {}
    neg_quality: dict[str, int] = {}
    heading_keys: set[str] = set()
    positive_doc_keys: set[str] = set()

    def scan(segment: str, in_heading: bool) -> None:
        if not segment:
            return
        for m in IDENTIFIER_PATTERN.finditer(segment):
            token = m.group(0)
            neg = (not in_heading) and is_negative_mention(segment, m.start())
            d_key = identifier_key(token)
            if not neg:
                positive_doc_keys.add(d_key)

            matched_targets: list[tuple[str, int]] = []
            if d_key in q_keys:
                q_rank = 3 if token.casefold() in raw_by_key[d_key] else 2
                matched_targets.append((d_key, q_rank))
            elif allow_ocr_fuzzy:
                o_key = ocr_identifier_key(token)
                for target_key in ocr_to_qkeys.get(o_key, ()):
                    matched_targets.append((target_key, 1))

            for target_key, rank in matched_targets:
                if neg:
                    if rank > neg_quality.get(target_key, 0):
                        neg_quality[target_key] = rank
                else:
                    if rank > pos_quality.get(target_key, 0):
                        pos_quality[target_key] = rank
                    if in_heading:
                        heading_keys.add(target_key)

    scan(heading_raw, True)
    scan(body or "", False)

    neg_only = frozenset(k for k in neg_quality if k not in pos_quality)
    effective_quality = dict(pos_quality)
    if allow_negative:
        for k in neg_only:
            effective_quality[k] = neg_quality[k]

    # Detect sibling modification conflict (e.g. doc is about Н-125М while query asks Н-125).
    sibling_conflict = False
    for k in neg_only:
        for dk in positive_doc_keys:
            if dk != k and (dk.startswith(k) or k.startswith(dk)):
                sibling_conflict = True
                break

    if not effective_quality:
        return IdentifierEvaluation(
            matched_keys=frozenset(),
            negative_only_keys=neg_only,
            heading_keys=frozenset(),
            match_type=None,
            sibling_variant_conflict=sibling_conflict,
            corroborating_hits=corroborating,
        )

    # Overall match_type is conservative: if ANY matched key required OCR-fuzzy or
    # homoglyph matching, surface that warning rather than claiming 'exact'.
    min_rank = min(effective_quality.values())
    match_type = {3: "exact", 2: "homoglyph_candidate", 1: "ocr_fuzzy_candidate"}[min_rank]
    return IdentifierEvaluation(
        matched_keys=frozenset(effective_quality),
        negative_only_keys=neg_only,
        heading_keys=frozenset(heading_keys),
        match_type=match_type,
        sibling_variant_conflict=sibling_conflict,
        corroborating_hits=corroborating,
    )


def build_pg_tsqueries(query: str) -> tuple[str, str]:
    """Build safe PostgreSQL OR tsquery expressions (russian, simple).

    All emitted terms consist strictly of alphanumeric characters joined by
    PostgreSQL tsquery operators (`|` and `<->`), eliminating syntax errors
    and injection vectors on arbitrary user input.
    """
    ru_terms: list[str] = []
    simple_terms: list[str] = []

    def add_unique(target: list[str], expr: str, limit: int = 24) -> None:
        if expr and expr not in target and len(target) < limit:
            target.append(expr)

    # 1. Compound tokens (preserve hyphenated/dotted codes like public-doc, Н-125, ИИ-017).
    for compound in _COMPOUND_TOKEN_PATTERN.findall(query):
        lowered = compound.casefold().replace("ё", "е")
        parts = [p for p in re.split(r"[.\-]+", lowered) if p]
        if not parts:
            continue
        if len(parts) > 1:
            # Adjacency query keeps compound codes tight without matching stray single parts.
            if all(p.isalnum() for p in parts):
                add_unique(simple_terms, "(" + " <-> ".join(parts) + ")")
                # Also add Cyrillic-confusable variant for Latin homoglyph codes (e.g. A-01).
                cyr = compound.upper().translate(IDENTIFIER_CONFUSABLE).casefold()
                cyr_parts = [p for p in re.split(r"[.\-]+", cyr) if p and p.isalnum()]
                if cyr_parts and cyr_parts != parts:
                    add_unique(simple_terms, "(" + " <-> ".join(cyr_parts) + ")")
        else:
            tok = parts[0]
            if not tok.isalnum():
                continue
            if tok in STOP_WORDS or not (len(tok) >= 2):
                continue
            if len(tok) > 2 or tok.isdecimal():
                add_unique(ru_terms, tok)
                add_unique(simple_terms, tok)
            elif tok.isalpha():
                add_unique(simple_terms, tok)

    # 2. For single-token identifiers with Latin lookalikes (e.g. 12X18H10T -> 12х18н10т),
    # add the Cyrillic candidate form to the simple tsquery so PG lexical can retrieve
    # candidate chunks before server-side homoglyph classification.
    for ident in extract_identifiers(query):
        cyr = ident.upper().translate(IDENTIFIER_CONFUSABLE).casefold()
        cyr_parts = [p for p in re.split(r"[.\-]+", cyr) if p and p.isalnum()]
        if len(cyr_parts) == 1:
            add_unique(simple_terms, cyr_parts[0])
        elif len(cyr_parts) > 1:
            add_unique(simple_terms, "(" + " <-> ".join(cyr_parts) + ")")

    # Fallback: if query had only stop words / very short tokens, keep safe alphanumeric terms.
    if not ru_terms and not simple_terms:
        for tok in re.findall(r"[0-9A-Za-zА-Яа-яЁё]+", query.casefold().replace("ё", "е")):
            if len(tok) >= 2:
                add_unique(simple_terms, tok)

    return " | ".join(ru_terms), " | ".join(simple_terms)


def format_verified_citations(answer: str, valid_citations: list[int]) -> str:
    """Keep validated inline [n] citations intact; strip forged/unvalidated [m] cleanly."""
    valid_set = set(valid_citations)

    def _keep_or_drop(match: re.Match[str]) -> str:
        num = int(match.group(1))
        return f"[{num}]" if num in valid_set else ""

    cleaned = _CITATION_TAG_PATTERN.sub(_keep_or_drop, answer)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([.,;:!?])", r"\1", cleaned).strip()
    missing = [n for n in valid_citations if f"[{n}]" not in cleaned]
    if missing:
        suffix = " ".join(f"[{n}]" for n in missing)
        cleaned = f"{cleaned} {suffix}".strip() if cleaned else suffix
    return cleaned


def _item_fields(item) -> tuple[str, str, str, str]:
    """Extract (title, designation, body, extraction_method) from dict or Hit-like object."""
    if isinstance(item, dict):
        return (
            str(item.get("title") or ""),
            str(item.get("designation") or ""),
            str(item.get("body") or item.get("text") or ""),
            "ocr" if item.get("extraction_method") == "ocr" else "text",
        )
    return (
        str(getattr(item, "title", "") or ""),
        str(getattr(item, "designation", "") or ""),
        str(getattr(item, "text", getattr(item, "body", "")) or ""),
        "ocr" if getattr(item, "extraction_method", "text") == "ocr" else "text",
    )


def citations_ground_identifiers(
    question: str | None,
    answer: str,
    cited_items: list,
) -> bool:
    """Verify that engineering identifiers in the question/answer exist in cited sources.

    Returns False if:
    - The model mentions an engineering identifier in `answer` that does not exist
      in any cited source (e.g. hallucinating ИИ-017 while citing DOC-001).
    - `question` requests specific engineering identifier(s) and one or more of them
      are completely absent from the union of cited sources.
    """
    if not cited_items:
        return False

    q_text = question or ""
    q_ids = extract_identifiers(q_text) if q_text else []
    a_ids = extract_identifiers(answer or "")
    if not q_ids and not a_ids:
        return True

    allow_neg = query_requests_comparison(q_text) if q_text else False

    def keys_supported_by_cited(probe_text: str) -> tuple[set[str], set[str]]:
        needed = {identifier_key(c) for c in extract_identifiers(probe_text)}
        if not needed:
            return set(), set()
        covered: set[str] = set()
        for item in cited_items:
            title, designation, body, method = _item_fields(item)
            ev = evaluate_identifier_match(
                probe_text,
                title,
                designation,
                body,
                extraction_method=method,
                allow_negative=allow_neg,
            )
            covered.update(ev.matched_keys)
            # For an OCR-cited item answering a concise generated phrase, also allow
            # positive OCR-fuzzy match against the cited source.
            if method == "ocr" and needed - covered:
                doc_ocr_keys: set[str] = set()
                for seg, in_head in ((f"{title} {designation}".strip(), True), (body, False)):
                    for m in IDENTIFIER_PATTERN.finditer(seg):
                        if not in_head and not allow_neg and is_negative_mention(seg, m.start()):
                            continue
                        doc_ocr_keys.add(ocr_identifier_key(m.group(0)))
                for code in extract_identifiers(probe_text):
                    if ocr_identifier_key(code) in doc_ocr_keys:
                        covered.add(identifier_key(code))
        return needed, covered

    if a_ids:
        a_needed, a_covered = keys_supported_by_cited(answer)
        if not a_needed.issubset(a_covered):
            return False

    if q_ids:
        q_needed, q_covered = keys_supported_by_cited(q_text)
        if not q_needed.issubset(q_covered):
            return False

    return True
