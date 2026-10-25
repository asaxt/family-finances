"""Local classifier decisions and Qwen handoffs; persistence stays in the app."""
import math
import re
import time
import unicodedata

from llm_evaluation import PREDICTION_FIELDS, validated_predictions


DEFAULT_CLASSIFIER_SCORE = 0.6


OTHER_CATEGORY = "Other"


INSTRUCTION_PATTERN = re.compile(
    r"\b(?:ignore|disregard|override|bypass|forget)\b[^.!?\n]{0,100}"
    r"\b(?:instructions?|prompts?|systems?|developers?|rules?|polic(?:y|ies)|categor(?:y|ies)|labels?|definitions?)\b"
    r"|\b(?:system|assistant|developer)\s*:|\[inst\]|<\|(?:system|assistant|developer)\|>"
    r"|\b(?:maximum|highest)\s+confidence\b"
    r"|\b(?:choose|assign|return|output)\b[^.!?\n]{0,80}\b(?:categor(?:y|ies)|labels?|confidence)\b"
)


BORROWING_PATTERN = re.compile(
    r"\b(?:loans?|borrow(?:ed|ing)?|disbursements?|line\s+of\s+credit|credit\s+line|cash\s+advance)\b"
)


def preview_predictions(rows, categories, score_rows, min_score=DEFAULT_CLASSIFIER_SCORE, min_margin=0.25):
    """Scores are ranking signals, not a probability of financial correctness."""
    if not 0 <= min_score <= 1 or not 0 <= min_margin <= 1:
        raise ValueError("Invalid preview thresholds.")
    if len(score_rows) != len(rows):
        raise ValueError("Classifier returned an invalid score count.")
    expected = {*categories, ""}
    predictions, rankings = [], []
    for row, scores in zip(rows, score_rows, strict=True):
        if set(scores) != expected or any(
            type(value) not in (int, float) or not 0 <= value <= 1
            or not math.isfinite(value) for value in scores.values()
        ):
            raise ValueError("Classifier returned invalid category scores.")
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
        category, score = ordered[0]
        margin = score - ordered[1][1]
        accepted = bool(category) and score >= min_score and margin >= min_margin
        predictions.append({
            "id": row["evaluation_id"], "category": category if accepted else "",
            "confidence": 1 if accepted else 0,
            "reason": "Category selected by the local classifier.",
        })
        rankings.append({"top_category": category, "score": score, "margin": margin})
    checked = validated_predictions(predictions, rows, categories)
    return [{**checked[row["evaluation_id"]], **ranking}
            for row, ranking in zip(rows, rankings, strict=True)]


def classifier_handoff_reason(row):
    """Route observed risky inputs to Qwen; these signals never choose a category."""
    text = unicodedata.normalize("NFKC", f"{row.get('merchant') or ''} {row.get('description') or ''}").casefold()
    if INSTRUCTION_PATTERN.search(text):
        return "embedded_instruction"
    amount = row.get("amount")
    incoming = (type(amount) in (int, float) and amount < 0) if amount is not None else row.get("direction") == "money_in"
    if incoming and BORROWING_PATTERN.search(text):
        return "incoming_loan"
    return ""


def hybrid_predictions(rows, categories, classifier_predictions, qwen_classify,
                       category_examples=None, fallback_category=OTHER_CATEGORY, use_handoff_guards=True):
    """Assign every valid result a category; uncertainty uses the existing Other label."""
    if not isinstance(fallback_category, str) or fallback_category.casefold() != "other":
        raise ValueError("The hybrid requires an existing Other category.")
    matches = [category for category in categories
               if isinstance(category, str) and category.casefold() == "other"]
    if len(matches) != 1:
        raise ValueError("The hybrid requires one existing Other category.")
    fallback_category = matches[0]
    metadata_fields = {"status", "top_category", "score", "margin"}
    if not isinstance(classifier_predictions, list):
        raise ValueError("Invalid classifier predictions.")
    raw, metadata = [], {}
    for item in classifier_predictions:
        if not isinstance(item, dict) or set(item) - (PREDICTION_FIELDS | metadata_fields):
            raise ValueError("Invalid classifier predictions.")
        if "top_category" in item and (
            not isinstance(item["top_category"], str) or item["top_category"] not in ["", *categories]
        ):
            raise ValueError("Invalid classifier ranking metadata.")
        for key in ("score", "margin"):
            if key in item and (type(item[key]) not in (int, float)
                                or not 0 <= item[key] <= 1 or not math.isfinite(item[key])):
                raise ValueError("Invalid classifier ranking metadata.")
        if "status" in item and (not isinstance(item["status"], str)
                                 or item["status"] not in {"categorized", "uncategorized"}):
            raise ValueError("Invalid classifier ranking metadata.")
        prediction = {key: item[key] for key in PREDICTION_FIELDS if key in item}
        raw.append(prediction)
        if isinstance(item.get("id"), str):
            metadata[item["id"]] = {key: item[key] for key in metadata_fields if key in item}
    checked = validated_predictions(raw, rows, categories)
    handoff_reasons = {row["evaluation_id"]: classifier_handoff_reason(row) if use_handoff_guards else ""
                       for row in rows}
    merged = {}
    for row in rows:
        identifier = row["evaluation_id"]
        item = checked[identifier]
        ranking = metadata[identifier]
        merged[identifier] = {
            **item, **ranking, "status": item["status"], "source": "classifier",
            "classifier_top_category": ranking.get("top_category", item["category"]),
            "classifier_score": ranking.get("score"), "classifier_margin": ranking.get("margin"),
        }
    unresolved = [row for row in rows if not checked[row["evaluation_id"]]["category"]
                  or handoff_reasons[row["evaluation_id"]]]
    forced_qwen_count = sum(bool(checked[row["evaluation_id"]]["category"])
                            and bool(handoff_reasons[row["evaluation_id"]]) for row in rows)
    qwen_seconds = 0.0
    request_count = 0
    model_abstention_count = 0
    for start in range(0, len(unresolved), 5):
        batch = unresolved[start:start + 5]
        tick = time.perf_counter()
        predictions = qwen_classify(
            "qwen3.8:27b", categories, batch,
            category_examples=category_examples or {}, fallback_category=fallback_category,
        )
        validated = validated_predictions(predictions, batch, categories)
        proposed = {item["id"]: item for item in predictions}
        qwen_seconds += time.perf_counter() - tick
        request_count += 1
        for row in batch:
            identifier = row["evaluation_id"]
            item = validated[identifier]
            has_category = bool(item["category"])
            model_abstention_count += not has_category
            category = item["category"] if has_category else fallback_category
            merged[identifier] = {
                **item,
                "category": category,
                "confidence": item["confidence"] if has_category else 1,
                "status": "categorized",
                "reason": item["reason"] if has_category else "Assigned to Other because no supported specific category was returned.",
                "source": "qwen", "top_category": category,
                "handoff_reason": handoff_reasons[identifier] or "classifier_uncertain",
                "qwen_proposed_category": proposed[identifier]["category"],
                "qwen_confidence": proposed[identifier]["confidence"],
                **{key: value for key, value in merged[identifier].items() if key.startswith("classifier_")},
            }
    predictions = [merged[row["evaluation_id"]] for row in rows]
    return {
        "predictions": predictions, "classifier_count": len(rows) - len(unresolved),
        "qwen_count": len(unresolved), "qwen_request_count": request_count,
        "qwen_seconds": qwen_seconds,
        "human_review_count": sum(not item["category"] for item in predictions),
        "other_count": sum(item["category"] == fallback_category for item in predictions),
        "model_abstention_count": model_abstention_count,
        "forced_qwen_count": forced_qwen_count,
    }
