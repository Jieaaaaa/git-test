"""Lossless passage partitioning and rendering of model-generated factual groups."""

import json
import math
import re


MAX_GROUPS = 128
MAX_FACTS_PER_GROUP = 128


def split_passages(text, target_chars=700, max_sections=12):
    if (not isinstance(text, str) or type(target_chars) is not int or target_chars < 1
            or type(max_sections) is not int or max_sections < 1):
        raise ValueError("invalid_passage_parameters")
    count = min(max_sections, max(1, math.ceil(len(text) / target_chars)))
    if count == 1:
        return [text]
    width = len(text) / count
    paragraphs = [match.end() for match in re.finditer(r"\n\s*\n", text)]
    boundaries = [match.end() for match in re.finditer(r"\n+|[。！？!?；;]|\.\s+", text)]
    cuts = [0]
    for index in range(1, count):
        ideal = round(width * index)
        lower = max(cuts[-1] + 1, ideal - width * 0.25)
        upper = min(len(text) - (count - index), ideal + width * 0.25)
        paragraph_choices = [position for position in paragraphs
                             if max(cuts[-1] + 1, ideal - width * 0.55) <= position
                             <= min(len(text) - (count - index), ideal + width * 0.55)]
        choices = paragraph_choices or [position for position in boundaries if lower <= position <= upper]
        cuts.append(min(choices, key=lambda position: abs(position - ideal)) if choices else ideal)
    cuts.append(len(text))
    return [text[start:end] for start, end in zip(cuts, cuts[1:])]


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_ledger_key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("invalid_json_constant")


def _field(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("invalid_ledger_field")
    return " ".join(value.split())


def parse_groups(response):
    if not isinstance(response, str):
        raise ValueError("ledger_response_required")
    response = response.strip()
    if response.startswith("```"):
        lines = response.splitlines()
        if (len(lines) < 3 or lines[0].strip().lower() not in ("```", "```json")
                or lines[-1].strip() != "```"):
            raise ValueError("invalid_ledger_json_fence")
        response = "\n".join(lines[1:-1])
    value = json.loads(response, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("ledger_object_required")
    groups = value.get("groups")
    if not isinstance(groups, list) or not 1 <= len(groups) <= MAX_GROUPS:
        raise ValueError("invalid_ledger_groups")
    parsed = []
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("invalid_ledger_group")
        subject = _field(group.get("subject"))
        facts = group.get("facts")
        if not isinstance(facts, list) or not 1 <= len(facts) <= MAX_FACTS_PER_GROUP:
            raise ValueError("invalid_ledger_facts")
        parsed.append((subject, [_field(fact) for fact in facts]))
    return parsed


def render_groups(groups):
    merged = {}
    seen = {}
    for subject, facts in groups:
        if (not isinstance(subject, str) or not subject.strip()
                or not isinstance(facts, (list, tuple)) or not facts):
            raise ValueError("invalid_render_group")
        if subject not in merged:
            merged[subject] = []
            seen[subject] = set()
        for fact in facts:
            if not isinstance(fact, str) or not fact.strip():
                raise ValueError("invalid_render_fact")
            if fact not in seen[subject]:
                seen[subject].add(fact)
                merged[subject].append(fact)
    return "\n".join(subject + ": " + "; ".join(facts) for subject, facts in merged.items())


def table_shape_errors(text):
    """Report obvious pipe-column mismatches without inferring or changing facts."""
    if not isinstance(text, str):
        return []
    errors = []
    for line_index, line in enumerate(text.splitlines(), 1):
        subject, separator, facts = line.partition(": ")
        if not separator or "|" not in subject or "|" not in facts:
            continue
        expected = subject.count("|") + 1
        for row_index, row in enumerate(facts.split("; "), 1):
            if "|" not in row:
                continue
            actual = row.count("|") + 1
            if actual != expected:
                errors.append("line %d row %d: expected %d columns from subject, got %d"
                              % (line_index, row_index, expected, actual))
                if len(errors) == 8:
                    return errors
    return errors
