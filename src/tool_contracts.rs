//! Frozen public MCP metadata plus strict request validation.
//!
//! The discovery document was captured from the PR #10 stdio server. Runtime
//! validation also applies model validators that JSON Schema cannot express.
use serde_json::Value;

const DEFINITIONS: &str = include_str!("../contracts/generated-tools.json");

pub fn tools() -> Vec<Value> {
    serde_json::from_str(DEFINITIONS).expect("checked-in MCP tool definitions must be valid JSON")
}

pub fn validate(name: &str, arguments: &Value) -> Result<(), &'static str> {
    let definitions = tools();
    let definition = definitions
        .iter()
        .find(|tool| tool["name"] == name)
        .ok_or("unknown_tool")?;
    // The original request model trims goal before applying its length bounds.
    let mut normalized = arguments.clone();
    if name == "start_task"
        && let Some(goal) = normalized.pointer_mut("/request/goal")
        && let Some(text) = goal.as_str()
    {
        *goal = Value::String(text.trim().to_owned());
    }
    let schema = &definition["inputSchema"];
    if !matches_schema(&normalized, schema, schema) {
        return Err("invalid_arguments");
    }
    if name == "start_task" && !valid_task(&normalized["request"]) {
        return Err("invalid_arguments");
    }
    Ok(())
}

fn matches_schema(value: &Value, schema: &Value, root: &Value) -> bool {
    if let Some(reference) = schema["$ref"].as_str() {
        let Some(target) = reference
            .strip_prefix('#')
            .and_then(|path| root.pointer(path))
        else {
            return false;
        };
        if !matches_schema(value, target, root) {
            return false;
        }
    }
    if let Some(alternatives) = schema["anyOf"].as_array()
        && !alternatives
            .iter()
            .any(|item| matches_schema(value, item, root))
    {
        return false;
    }
    if let Some(options) = schema["enum"].as_array()
        && !options.contains(value)
    {
        return false;
    }
    if let Some(constant) = schema.get("const")
        && constant != value
    {
        return false;
    }
    if let Some(kind) = schema["type"].as_str() {
        let correct_type = match kind {
            "object" => value.is_object(),
            "array" => value.is_array(),
            "string" => value.is_string(),
            "integer" => value.is_i64() || value.is_u64(),
            "number" => value.is_number(),
            "boolean" => value.is_boolean(),
            "null" => value.is_null(),
            _ => false,
        };
        if !correct_type {
            return false;
        }
    }
    if let Some(object) = value.as_object() {
        if let Some(required) = schema["required"].as_array()
            && required
                .iter()
                .any(|name| name.as_str().is_none_or(|name| !object.contains_key(name)))
        {
            return false;
        }
        for (key, value) in object {
            if let Some(property) = schema["properties"].get(key) {
                if !matches_schema(value, property, root) {
                    return false;
                }
            } else {
                match schema.get("additionalProperties") {
                    Some(Value::Bool(false)) => return false,
                    Some(additional)
                        if additional.is_object() && !matches_schema(value, additional, root) =>
                    {
                        return false;
                    }
                    _ => {}
                }
            }
        }
    }
    if let Some(items) = value.as_array() {
        if !length_matches(items.len(), schema, "minItems", "maxItems") {
            return false;
        }
        if let Some(item_schema) = schema.get("items")
            && !items
                .iter()
                .all(|item| matches_schema(item, item_schema, root))
        {
            return false;
        }
    }
    if let Some(text) = value.as_str() {
        if !length_matches(text.chars().count(), schema, "minLength", "maxLength") {
            return false;
        }
        if let Some(pattern) = schema["pattern"].as_str() {
            let valid = match pattern {
                "^[A-Za-z0-9_-]{1,128}$" => {
                    !text.is_empty()
                        && text.len() <= 128
                        && text
                            .bytes()
                            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
                }
                "^[a-f0-9]{64}$" => {
                    text.len() == 64
                        && text
                            .bytes()
                            .all(|byte| byte.is_ascii_digit() || matches!(byte, b'a'..=b'f'))
                }
                _ => false,
            };
            if !valid {
                return false;
            }
        }
    }
    if let Some(number) = value.as_f64() {
        for (keyword, passes) in [
            (
                "minimum",
                schema["minimum"]
                    .as_f64()
                    .is_none_or(|limit| number >= limit),
            ),
            (
                "maximum",
                schema["maximum"]
                    .as_f64()
                    .is_none_or(|limit| number <= limit),
            ),
            (
                "exclusiveMinimum",
                schema["exclusiveMinimum"]
                    .as_f64()
                    .is_none_or(|limit| number > limit),
            ),
            (
                "exclusiveMaximum",
                schema["exclusiveMaximum"]
                    .as_f64()
                    .is_none_or(|limit| number < limit),
            ),
        ] {
            if schema.get(keyword).is_some() && !passes {
                return false;
            }
        }
    }
    true
}

fn length_matches(length: usize, schema: &Value, minimum: &str, maximum: &str) -> bool {
    schema[minimum]
        .as_u64()
        .is_none_or(|bound| length as u64 >= bound)
        && schema[maximum]
            .as_u64()
            .is_none_or(|bound| length as u64 <= bound)
}

fn valid_task(task: &Value) -> bool {
    let Some(url) = task["start_url"].as_str() else {
        return false;
    };
    if !valid_http_url(url) {
        return false;
    }
    let Some(values) = task["values"].as_object() else {
        return false;
    };
    if values.len() > 32
        || values.iter().any(|(key, value)| {
            key.chars().count() > 128
                || value
                    .as_str()
                    .is_none_or(|text| text.chars().count() > 4000)
        })
    {
        return false;
    }
    let Some(checks) = task["checks"].as_array() else {
        return false;
    };
    if !checks.iter().all(valid_check) {
        return false;
    }
    if let Some(literals) = task
        .pointer("/privacy/blocked_literals")
        .and_then(Value::as_array)
        && literals.iter().any(|literal| {
            literal
                .as_str()
                .is_none_or(|text| !(1..=256).contains(&text.chars().count()))
        })
    {
        return false;
    }
    true
}

fn valid_http_url(text: &str) -> bool {
    if text.chars().any(|ch| ch.is_whitespace() || ch.is_control()) {
        return false;
    }
    let Ok(url) = reqwest::Url::parse(text) else {
        return false;
    };
    if !matches!(url.scheme(), "http" | "https")
        || url.host_str().is_none()
        || url.port() == Some(0)
    {
        return false;
    }
    // Even an empty username marker is forbidden by the published contract.
    let authority = text
        .split_once("://")
        .map(|(_, rest)| rest.split(['/', '?', '#']).next().unwrap_or_default());
    authority.is_some_and(|authority| !authority.contains('@'))
        && url.username().is_empty()
        && url.password().is_none()
}

fn valid_check(check: &Value) -> bool {
    let Some(kind) = check["kind"].as_str() else {
        return false;
    };
    let target = check
        .get("target")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if target.len() > 512 {
        return false;
    }
    if kind == "route" {
        if !target.is_empty() {
            return false;
        }
    } else if target.is_empty() || rejected_selector(target) {
        return false;
    }
    let expected = &check["expected"];
    match kind {
        "route" | "text" | "value" => expected
            .as_str()
            .is_some_and(|text| text.chars().count() <= 4000 && text.len() <= 4096),
        "visible" => expected.is_boolean(),
        "count" => expected.as_u64().is_some(),
        _ => false,
    }
}

fn rejected_selector(target: &str) -> bool {
    if target.contains(['\n', '\r', '\0']) || target.contains(">>") {
        return true;
    }
    if let Some((prefix, _)) = target.split_once('=')
        && !prefix.is_empty()
        && prefix
            .bytes()
            .all(|byte| byte.is_ascii_alphabetic() || matches!(byte, b'_' | b'-'))
    {
        return true;
    }
    let lowered = target.to_lowercase();
    lowered.starts_with("//")
        || ["xpath=", "text=", ":has-text(", ":text(", "internal:"]
            .iter()
            .any(|marker| lowered.contains(marker))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn request() -> Value {
        json!({"request": {"goal":"Read the page", "start_url":"https://example.test/", "values":{}, "checks":[], "limits":{}, "privacy":{}}})
    }

    #[test]
    fn advertises_exactly_the_published_six_tools() {
        let definitions = tools();
        assert_eq!(definitions.len(), 6);
        for name in [
            "start_task",
            "get_task",
            "cancel_task",
            "read_artifact",
            "capabilities",
            "present_admission",
        ] {
            let tool = definitions
                .iter()
                .find(|tool| tool["name"] == name)
                .unwrap();
            assert!(tool["description"].as_str().unwrap().len() > 150);
            assert_eq!(tool["inputSchema"]["additionalProperties"], false);
            assert_eq!(tool["outputSchema"]["type"], "object");
            assert_eq!(
                tool["annotations"]["readOnlyHint"],
                matches!(name, "get_task" | "read_artifact" | "capabilities")
            );
        }
    }

    #[test]
    fn minimal_requests_defaults_and_optional_run_recovery_are_valid() {
        assert_eq!(validate("start_task", &request()), Ok(()));
        for args in [json!({}), json!({"run_id":null}), json!({"run_id":"null"})] {
            assert_eq!(validate("get_task", &args), Ok(()));
        }
        assert_eq!(
            validate(
                "read_artifact",
                &json!({"run_id":"run-1","artifact_id":"a_1"})
            ),
            Ok(())
        );
        assert_eq!(validate("capabilities", &json!({})), Ok(()));
        assert_eq!(validate("unsupported", &json!({})), Err("unknown_tool"));
    }

    #[test]
    fn rejects_unknown_fields_nulls_and_json_encoded_objects() {
        for args in [
            Value::Null,
            json!([]),
            json!({"approval":true}),
            json!("{}"),
        ] {
            assert_eq!(validate("capabilities", &args), Err("invalid_arguments"));
        }
        for pointer in ["/request/limits", "/request/privacy", "/request"] {
            let mut args = request();
            args.pointer_mut(pointer).unwrap()["grant"] = json!(true);
            assert!(validate("start_task", &args).is_err());
        }
        let mut args = request();
        args["request"]["checks"] = Value::Null;
        assert!(validate("start_task", &args).is_err());
        args = request();
        args["request"] = json!(args["request"].to_string());
        assert!(validate("start_task", &args).is_err());
    }

    #[test]
    fn rejects_invalid_identifiers_digests_and_byte_ranges() {
        for id in ["", "../run", "r.u.n", "⚠", "run\n"] {
            assert!(validate("get_task", &json!({"run_id":id})).is_err());
            assert!(validate("cancel_task", &json!({"run_id":id})).is_err());
        }
        for digest in ["A".repeat(64), "f".repeat(63), "g".repeat(64)] {
            assert!(
                validate(
                    "present_admission",
                    &json!({"run_id":"r","proposal_id":"p","digest":digest})
                )
                .is_err()
            );
        }
        for (key, value) in [
            ("offset", json!(-1)),
            ("offset", json!(1.0)),
            ("limit", json!(0)),
            ("limit", json!(65537)),
            ("limit", json!(true)),
            ("channel", json!("url")),
        ] {
            let mut args = json!({"run_id":"r","artifact_id":"a"});
            args[key] = value;
            assert!(validate("read_artifact", &args).is_err(), "{args}");
        }
    }

    #[test]
    fn rejects_unsafe_urls_and_trims_goal_before_length_validation() {
        for url in [
            "https://example.test:0/",
            "https://example.test:65536/",
            "https://@example.test/",
            "https://u:p@example.test",
            "https://example.test/\n",
            "https://example.test/\u{0085}",
            "file:///tmp/a",
            "/relative",
        ] {
            let mut args = request();
            args["request"]["start_url"] = json!(url);
            assert!(validate("start_task", &args).is_err(), "{url:?}");
        }
        for url in ["https://[::1]:65535/a?q=1#s", "http://localhost:1/"] {
            let mut args = request();
            args["request"]["start_url"] = json!(url);
            assert!(validate("start_task", &args).is_ok());
        }
        let mut args = request();
        args["request"]["goal"] = json!(" ".repeat(4001) + "read");
        assert!(validate("start_task", &args).is_ok());
        args["request"]["goal"] = json!("\n  ");
        assert!(validate("start_task", &args).is_err());
    }

    #[test]
    fn enforces_nested_bounds_and_strict_scalar_types() {
        for (key, value) in [
            ("max_steps", json!(0)),
            ("max_steps", json!(1001)),
            ("max_steps", json!(1.0)),
            ("max_steps", json!(true)),
            ("max_seconds", json!(0)),
            ("max_seconds", json!(601)),
            ("max_seconds", json!("1")),
            ("max_stored_bytes", json!(104857601)),
        ] {
            let mut args = request();
            args["request"]["limits"][key] = value;
            assert!(validate("start_task", &args).is_err(), "{args}");
        }
        let mut args = request();
        args["request"]["viewports"] = json!([]);
        assert!(validate("start_task", &args).is_err());
        args["request"]["viewports"] = json!([{"width":100,"height":4096}]);
        assert!(validate("start_task", &args).is_ok());
        args["request"]["privacy"]["blocked_literals"] = json!([""]);
        assert!(validate("start_task", &args).is_err());
        args["request"]["privacy"]["blocked_literals"] = json!(["private"]);
        assert!(validate("start_task", &args).is_ok());
    }

    #[test]
    fn validates_checks_as_independent_typed_css_queries() {
        let checks = [
            json!({"kind":"route","expected":"https://example.test/"}),
            json!({"kind":"text","target":"#status","expected":"ready"}),
            json!({"kind":"value","target":"input[name=x]","expected":"yes"}),
            json!({"kind":"visible","target":".dialog","expected":false}),
            json!({"kind":"count","target":"li","expected":0}),
        ];
        let mut args = request();
        args["request"]["checks"] = json!(checks);
        assert!(validate("start_task", &args).is_ok());
        for check in [
            json!({"kind":"fixture_state","expected":"ready"}),
            json!({"kind":"route","target":"#status","expected":"/"}),
            json!({"kind":"text","target":"#status","expected":true}),
            json!({"kind":"count","target":"li","expected":1.0}),
            json!({"kind":"visible","target":".dialog","expected":1}),
            json!({"kind":"text","target":"xpath=//a","expected":"yes"}),
            json!({"kind":"text","target":"#x >> button","expected":"yes"}),
            json!({"kind":"text","target":"é".repeat(257),"expected":"yes"}),
            json!({"kind":"text","target":"#x","expected":"é".repeat(2049)}),
        ] {
            args["request"]["checks"] = json!([check]);
            assert!(validate("start_task", &args).is_err(), "{args}");
        }
    }
}
