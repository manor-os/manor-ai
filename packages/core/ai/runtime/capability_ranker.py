"""Local ranking of the capability catalog for discovery, with no model call.

Tool, Skill, provider and operation names and descriptions are English, so the
searching model writes its query in English keywords whatever language the
user uses, and one BM25 ranking serves every language. The model that asked
reads the ranked shortlist and decides; ranking is recall, not a suitability
verdict. It replaced a utility-model matcher that took 4.4-10s per search on
staging and timed out into an unranked catalog dump.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from functools import lru_cache
from typing import Any, Iterable, Mapping, Sequence

from packages.core.ai.runtime.capability_matcher import MAX_MATCHES, CapabilitySelection, candidate_id
from packages.core.ai.runtime.capability_search_metadata import capability_search_terms
from packages.core.ai.runtime.capability_search import _SEARCH_STOPWORDS

_IDENTIFIER_BREAK = re.compile(r"(?<=[a-z])(?=[A-Z])|[^A-Za-z0-9]+")
_ENGLISH_WORD = re.compile(r"[A-Za-z]{2,}")
_NAME_WEIGHT = 3
# A query that names a capability ("Chrome", "an X post", "gmail") should reach
# it however long the rest of the sentence is: catalog-rare filler words
# ("professional", "live") otherwise outscore the one word that names it.
_DESCRIPTION_CHARS = 2_000
_K1 = 1.2
_B = 0.75


def query_is_english(query: str) -> bool:
    """English keywords only: any other script is sent back to be rewritten.

    A mostly-Chinese query with one brand name would rank on the brand alone.
    """
    text = str(query or "")
    return bool(_ENGLISH_WORD.search(text)) and all(ch.isascii() for ch in text if ch.isalpha())


# Words that end in "s" without being plurals; stripping it would merge
# "news" into "new" and "canvas" into the Canva provider.
_NOT_PLURAL = frozenset({"news", "canvas", "atlas", "alias", "series", "lens", "always", "perhaps", "whereas"})
# Function words never name a capability, yet ones rare in the catalog ("my")
# get a high IDF and outrank the words that do ("schedule").
_FUNCTION_WORDS = _SEARCH_STOPWORDS | frozenset({
    "me", "my", "mine", "you", "your", "yours", "we", "us", "our", "ours", "they", "them", "their",
    "he", "him", "his", "she", "her", "its", "i", "did", "done", "can", "could", "should", "would",
    "will", "shall", "may", "might", "must", "please", "any", "some", "there", "here", "these",
    "those", "than", "then", "also", "just", "only", "very", "via", "about", "including", "if",
    "so", "but", "not", "no", "all", "each", "every", "both", "own", "was", "were", "been", "being",
    # Request verbs: "show me", "I want", "help me" ask for anything.
    "show", "give", "tell", "want", "need", "let", "help",
})
# One-letter English words; any other capital letter standing alone is a
# name (X, R, C).
_ONE_LETTER_WORDS = frozenset({"a", "i"})


_VOWELS = frozenset("aeiouy")


def _short_cvc(stem: str) -> bool:
    """A three-letter consonant-vowel-consonant stem lost its "e" (mak-ing)."""
    return (
        len(stem) == 3 and stem[0] not in _VOWELS and stem[1] in _VOWELS
        and stem[2] not in _VOWELS and stem[2] not in "wxy"
    )


def _stem(term: str) -> str:
    """Plurals, -ing/-ed and a final -e, applied alike to queries and catalog
    text so "creating", "created" and "create" meet. Light on purpose:
    aggressive suffix stripping merges unrelated words."""
    if term in _NOT_PLURAL:
        return term
    if len(term) > 4 and term.endswith("ies"):
        term = term[:-3] + "y"
    elif term.endswith(("sses", "xes", "zes", "ches", "shes")):
        term = term[:-2]
    elif len(term) > 3 and term.endswith("s") and not term.endswith(("ss", "us", "is")):
        term = term[:-1]
    for suffix in ("ing",):
        stem = term[:-len(suffix)]
        if not term.endswith(suffix) or len(stem) < 3 or not _VOWELS & set(stem):
            continue
        if len(stem) >= 4 and stem[-1] == stem[-2] and stem[-1] not in _VOWELS | set("lsz"):
            term = stem[:-1]  # logging -> log, embedded -> embed
        elif len(stem) > 3 or stem.endswith("x"):
            term = stem  # fixed -> fix
        elif _short_cvc(stem):
            term = stem + "e"  # making -> make, named -> name
        break
    if len(term) >= 5 and term.endswith("e"):
        term = term[:-1]
    return term


def ranking_terms(text: str) -> list[str]:
    """English terms of a name or description; identifiers split into words."""
    return list(_ranking_terms(str(text or "")))


def _kept(part: str, *, identifier: bool) -> bool:
    """A one-letter part is a name: always in an identifier (twitter_x),
    in prose only as a capital standing alone ("an X post", not "e.g.")."""
    if not part.isascii() or not part.isalpha() and len(part) == 1:
        return False
    if len(part) > 1:
        return True
    return part.casefold() not in _ONE_LETTER_WORDS and (identifier or part.isupper())


@lru_cache(maxsize=16_384)
def _ranking_terms(text: str, identifier: bool = False) -> tuple[str, ...]:
    return tuple(
        _stem(part.casefold())
        for part in _IDENTIFIER_BREAK.split(text)
        if part and _kept(part, identifier=identifier) and part.casefold() not in _FUNCTION_WORDS
    )


@lru_cache(maxsize=8_192)
def _document(identity: str, description: str, details: str) -> tuple[tuple[tuple[str, int], ...], frozenset[str]]:
    """Term frequencies of one catalog entry and the terms naming it, computed once per content."""
    frequencies = Counter(_ranking_terms(description[:_DESCRIPTION_CHARS]))
    frequencies.update(_ranking_terms(details))
    named = _ranking_terms(identity, identifier=True)
    for term in named:
        frequencies[term] += _NAME_WEIGHT
    return tuple(frequencies.items()), frozenset(named)


def _document_for(item: Mapping[str, Any], key: str) -> tuple[Counter, frozenset[str]]:
    identity = " ".join(dict.fromkeys(filter(None, (
        str(item.get("name") or ""), str(item.get("display_name") or ""), str(item.get("slug") or ""),
        key.split(":", 1)[-1] if ":" in key else "",
        # Declared words people use for it (capability_search_metadata).
        " ".join(item.get("search_terms") or capability_search_terms(key)),
    ))))
    details = " ".join(filter(None, (
        " ".join(str(p) for p in item.get("parameters") or () if isinstance(p, str)),
        str(item.get("category") or ""),
    )))
    frequencies, named = _document(identity, str(item.get("description") or ""), details)
    return Counter(dict(frequencies)), named


# "+chrome fill form": the capability's name or declared search terms must
# contain "chrome"; the other words only rank what qualifies.
_REQUIRED_WORD = re.compile(r"(?<!\S)\+([^\s+]+)")


def _search_terms(query: str) -> tuple[list[str], frozenset[str]]:
    """A query's ranking terms and the ones it requires (written +word)."""
    required = [term for word in _REQUIRED_WORD.findall(query) for term in _ranking_terms(word, identifier=True)]
    return list(dict.fromkeys([*required, *ranking_terms(query)])), frozenset(required)


def rank_runtime_capabilities(
    *, query: str, candidates: Iterable[Mapping[str, Any]], limit: int = MAX_MATCHES,
    queries: Sequence[str] = (),
) -> CapabilitySelection:
    """BM25 over names (weighted) and descriptions; the best ``limit`` hits.

    ``queries`` holds one query per step of a request that needs several
    capabilities: each is ranked on its own and the rankings interleave, so
    every step reaches the shortlist and each match names the query it serves.
    """
    searches = [text for text in (str(q or "").strip() for q in queries) if text] or [query]
    parsed = [(text, *_search_terms(text)) for text in searches]
    all_terms = {term for _, terms, _ in parsed for term in terms}
    documents: list[tuple[str, Counter, frozenset[str]]] = []
    seen: set[str] = set()
    for item in candidates:
        key = candidate_id(dict(item))
        if key in seen:
            continue
        seen.add(key)
        documents.append((key, *_document_for(item, key)))
    if not documents or not all_terms:
        return CapabilitySelection("unmatched", examined=len(documents))
    average_length = sum(sum(tf.values()) for _, tf, _ in documents) / len(documents) or 1.0
    document_frequency = Counter(term for _, tf, _ in documents for term in set(tf) if term in all_terms)

    def ranked(terms: list[str], required: frozenset[str]) -> list[tuple[str, list[str]]]:
        scored = []
        for key, tf, named in documents:
            if not required <= named:
                continue
            length = sum(tf.values())
            hits = [term for term in terms if tf.get(term)]
            if not hits:
                continue
            score = 0.0
            for term in hits:
                idf = math.log(1 + (len(documents) - document_frequency[term] + 0.5) / (document_frequency[term] + 0.5))
                score += idf * tf[term] * (_K1 + 1) / (tf[term] + _K1 * (1 - _B + _B * length / average_length))
            scored.append((score, key, hits))
        scored.sort(key=lambda entry: (-entry[0], entry[1]))
        return [(key, hits) for _score, key, hits in scored]

    rankings = [[(key, hits, text) for key, hits in ranked(terms, required)] for text, terms, required in parsed]
    order: dict[str, tuple[list[str], str]] = {}
    for position in range(max(map(len, rankings), default=0)):
        for ranking in rankings:
            if position < len(ranking):
                key, hits, text = ranking[position]
                order.setdefault(key, (hits, text))
    matches = tuple(
        {"id": key, "assessment": {
            "source": "lexical_rank", "fit": "unassessed", "rank": rank,
            "reason": (
                f"Matches {', '.join(hits)} in its name or description; read the "
                "description and schema to decide whether it does the job."
            ),
            **({"query": text} if len(searches) > 1 else {}),
        }}
        for rank, (key, (hits, text)) in enumerate(list(order.items())[:limit])
    )
    return CapabilitySelection("ranked" if matches else "unmatched", matches, None, len(documents))
