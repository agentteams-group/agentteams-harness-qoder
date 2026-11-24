"""Lossless conversion between native forms and shared question replies."""
import json

from jsonschema import Draft202012Validator


def validate_answers(questions, responses):
    if not isinstance(responses, list) or len(responses) != len(questions):
        raise ValueError("Answer every question exactly once")
    answers = {}
    for question in questions:
        matches = [row for row in responses if isinstance(row, dict) and row.get("id") == question["id"]]
        if len(matches) != 1:
            raise ValueError("Question ID is missing or duplicated")
        row = matches[0]
        selected, custom = row.get("selected", []), row.get("custom", "")
        options = {option["id"] for option in question.get("options", [])}
        if (not isinstance(selected, list) or any(not isinstance(value, str) or value not in options for value in selected)
                or len(set(selected)) != len(selected) or not isinstance(custom, str)
                or (len(selected) > 1 and not question.get("multi_select"))
                or (bool(selected) == bool(custom.strip()))):
            raise ValueError("Question answer or option is invalid")
        answers[question["id"]] = {"selected": selected, "custom": custom.strip()}
    return answers


def form_questions(schema):
    Draft202012Validator.check_schema(schema)
    if schema.get("type") != "object" or not schema.get("properties"):
        raise ValueError("A native form must declare object properties")
    questions = []
    for name, field in schema["properties"].items():
        question = {"id": name, "question": field.get("description") or field.get("title") or name}
        values = field.get("enum", [False, True] if field.get("type") == "boolean" else None)
        if field.get("type") == "array":
            values = field.get("items", {}).get("enum")
            question["multi_select"] = True
        if values is not None:
            question["options"] = [{"id": str(index), "label": str(value)} for index, value in enumerate(values)]
        if field.get("type") in {"object", "array"} and values is None:
            question["question"] += " (JSON)"
        if name not in schema.get("required", []):
            question.setdefault("options", []).append({"id": "skip", "label": "不填写"})
        questions.append(question)
    return questions


def form_answer(schema, questions, responses):
    answers = validate_answers(questions, responses)
    content = {}
    for name, row in answers.items():
        if row["selected"] == ["skip"]:
            continue
        field = schema["properties"][name]
        values = field.get("enum", [False, True] if field.get("type") == "boolean" else None)
        if field.get("type") == "array":
            values = field.get("items", {}).get("enum")
        if row["selected"]:
            chosen = [values[int(index)] for index in row["selected"]]
            content[name] = chosen if field.get("type") == "array" else chosen[0]
        else:
            content[name] = row["custom"] if field.get("type") == "string" else json.loads(row["custom"])
    # MCP forms are self-contained. Never fetch schema references from the network.
    def has_reference(value):
        if isinstance(value, dict):
            return "$ref" in value or any(has_reference(item) for item in value.values())
        return isinstance(value, list) and any(has_reference(item) for item in value)
    if has_reference(schema):
        raise ValueError("Referenced form schemas are unsupported")
    error = next(Draft202012Validator(schema).iter_errors(content), None)
    if error:
        raise ValueError("The answer does not satisfy the native form schema")
    return content
