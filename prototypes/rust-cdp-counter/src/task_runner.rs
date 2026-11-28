use crate::browser::{BrowserResult, MatrixPage, run_task_scenario};
use crate::provider::Provider;
use serde_json::{Value, json};
use std::time::{Duration, Instant};

pub trait RunControl {
    fn cancelled(&self) -> bool;
    fn approve(&self, proposal: Value) -> Result<(), &'static str>;
}

pub struct TaskOutcome {
    pub terminal: String,
    pub functional: String,
    pub visual_diagnostic: String,
    pub reason: Option<String>,
    pub last_url: Option<String>,
    pub usage: Value,
    pub timings: Value,
    pub evidence: Vec<String>,
    pub artifacts: Vec<(String, Vec<u8>)>,
}

#[derive(Clone)]
struct Action {
    operation: String,
    target: String,
    value: Option<String>,
    description: String,
    context: String,
}

struct Budget {
    deadline: Instant,
    steps: u64,
    calls: u64,
    input_bytes: usize,
    output_bytes: usize,
    max_steps: u64,
    max_calls: u64,
    max_input: usize,
    max_output: usize,
    max_stored: usize,
}

fn bounded(request: &Value, key: &str, default: u64, upper: u64) -> BrowserResult<u64> {
    match request["limits"].get(key) {
        Some(value) => value
            .as_u64()
            .filter(|v| (1..=upper).contains(v))
            .ok_or_else(|| key.into()),
        None => Ok(default),
    }
}

fn reject<T>(reason: &'static str) -> BrowserResult<T> {
    Err(reason.into())
}

fn alive(control: &dyn RunControl, deadline: Instant) -> BrowserResult<()> {
    if control.cancelled() {
        return reject("cancelled");
    }
    if Instant::now() >= deadline {
        return reject("budget_limit");
    }
    Ok(())
}

fn private_text(value: &Value, blocked: &[String]) -> BrowserResult<()> {
    let contains = |text: &str| blocked.iter().any(|literal| text.contains(literal));
    match value {
        Value::String(text) if contains(text) => reject("privacy_blocked"),
        Value::Array(items) => items
            .iter()
            .try_for_each(|item| private_text(item, blocked)),
        Value::Object(items) => {
            for (key, item) in items {
                if contains(key) {
                    return reject("privacy_blocked");
                }
                private_text(item, blocked)?;
            }
            Ok(())
        }
        _ => Ok(()),
    }
}

fn actions(snapshot: &Value, values: &Value) -> BrowserResult<Vec<Action>> {
    let mut actions = Vec::new();
    let choices = snapshot["choices"].as_array().ok_or("observation_failed")?;
    let values = values.as_object().ok_or("invalid_request")?;
    for choice in choices {
        let Some(operation) = choice["operation"].as_str() else {
            continue;
        };
        let Some(target) = choice["id"].as_str() else {
            continue;
        };
        let label = choice["label"].as_str().unwrap_or_default();
        let context = choice["context"].as_str().unwrap_or_default().to_owned();
        let current = choice["current"].as_str().unwrap_or_default();
        match operation {
            "fill" => {
                let provided = values.get(label).or_else(|| {
                    choice["aliases"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(Value::as_str)
                        .find_map(|a| values.get(a))
                });
                if let Some(value) = provided.and_then(Value::as_str).filter(|v| *v != current) {
                    actions.push(Action {
                        operation: operation.to_owned(),
                        target: target.to_owned(),
                        value: Some(value.to_owned()),
                        description: format!("Fill {label:?} with its provided value"),
                        context,
                    });
                }
            }
            "select" => {
                for option in choice["options"].as_array().into_iter().flatten() {
                    let Some(value) = option["value"].as_str() else {
                        continue;
                    };
                    if option["disabled"] == true || value == current || value.is_empty() {
                        continue;
                    }
                    actions.push(Action {
                        operation: operation.to_owned(),
                        target: target.to_owned(),
                        value: Some(value.to_owned()),
                        description: format!("Select {:?} for {label:?}", option["label"]),
                        context: context.clone(),
                    });
                }
            }
            "click" => actions.push(Action {
                operation: operation.to_owned(),
                target: target.to_owned(),
                value: None,
                description: format!("Click {label:?}"),
                context,
            }),
            _ => {}
        }
    }
    for direction in ["down", "up"] {
        if snapshot["scroll"][direction] == true {
            actions.push(Action {
                operation: "scroll".to_owned(),
                target: String::new(),
                value: Some(direction.to_owned()),
                description: format!("Scroll {direction}"),
                context: String::new(),
            });
        }
    }
    actions.push(Action {
        operation: "wait".to_owned(),
        target: String::new(),
        value: None,
        description: "Wait for a changing page".to_owned(),
        context: String::new(),
    });
    if actions.len() > 255 {
        return reject("too_many_choices");
    }
    Ok(actions)
}

fn deferred(action: &Action) -> bool {
    let text = format!("{} {}", action.description, action.context).to_lowercase();
    [
        "purchase",
        "buy now",
        "pay now",
        "place order",
        "transfer",
        "send message",
        "send email",
        "book now",
        "confirm booking",
        "delete",
        "publish",
        "checkout",
        "payment",
        "оплатить",
        "купить",
        "перевести",
        "забронировать",
        "удалить",
        "опубликовать",
    ]
    .iter()
    .any(|word| text.contains(word))
}

fn checks(page: &mut MatrixPage, requested: &[Value]) -> BrowserResult<bool> {
    page.read_public_checks(requested)
}

pub fn execute(request: &Value, provider: Provider, control: &dyn RunControl) -> TaskOutcome {
    let started = Instant::now();
    let mut outcome = TaskOutcome {
        terminal: "failed".to_owned(),
        functional: "not_run".to_owned(),
        visual_diagnostic: if request["visual"] == true {
            "not_run"
        } else {
            "not_requested"
        }
        .to_owned(),
        reason: None,
        last_url: None,
        usage: json!({"calls":0,"steps":0,"input_bytes":0,"output_bytes":0,"stored_bytes":0}),
        timings: json!({}),
        evidence: Vec::new(),
        artifacts: Vec::new(),
    };
    let mut counters = None;
    let result = (|| -> BrowserResult<()> {
        let mode = request["mode"].as_str().unwrap_or("isolated");
        if mode != "isolated" {
            return reject("unsupported_mode");
        }

        let goal = request["goal"]
            .as_str()
            .filter(|s| !s.trim().is_empty() && s.chars().count() <= 4000)
            .ok_or("invalid_request")?;
        let url = request["start_url"].as_str().ok_or("invalid_request")?;
        let parsed = reqwest::Url::parse(url).map_err(|_| "invalid_request")?;
        if !matches!(parsed.scheme(), "http" | "https")
            || !parsed.username().is_empty()
            || parsed.password().is_some()
            || parsed.host_str().is_none()
            || url.chars().any(char::is_whitespace)
        {
            return reject("invalid_request");
        }
        if parsed.scheme() != "http" || parsed.host_str() != Some("127.0.0.1") {
            return reject("unsupported_origin");
        }
        let requested = request["checks"]
            .as_array()
            .filter(|a| a.len() <= 32)
            .ok_or("invalid_request")?;
        let values = request["values"]
            .as_object()
            .filter(|v| {
                v.len() <= 32
                    && v.iter().all(|(k, v)| {
                        k.chars().count() <= 128
                            && v.as_str().is_some_and(|s| s.chars().count() <= 4000)
                    })
            })
            .ok_or("invalid_request")?;
        if !request["privacy"].is_object() || !request["limits"].is_object() {
            return reject("invalid_request");
        }
        let mut blocked = Vec::new();
        if let Some(items) = request["privacy"].get("blocked_literals") {
            for literal in items
                .as_array()
                .filter(|a| a.len() <= 32)
                .ok_or("invalid_request")?
            {
                blocked.push(
                    literal
                        .as_str()
                        .filter(|s| !s.is_empty() && s.chars().count() <= 256)
                        .ok_or("invalid_request")?
                        .to_owned(),
                );
            }
        }
        private_text(
            &json!({"goal":goal,"values":values,"start_url":url}),
            &blocked,
        )?;
        let seconds = request["limits"]
            .get("max_seconds")
            .map_or(Some(120.0), Value::as_f64)
            .filter(|s| s.is_finite() && *s > 0.0 && *s <= 600.0)
            .ok_or("invalid_request")?;
        let mut budget = Budget {
            deadline: started + Duration::from_secs_f64(seconds),
            steps: 0,
            calls: 0,
            input_bytes: 0,
            output_bytes: 0,
            max_steps: bounded(request, "max_steps", 12, 1000)?,
            max_calls: bounded(request, "max_provider_calls", 12, 1000)?,
            max_input: bounded(request, "max_input_bytes", 262144, 16777216)? as usize,
            max_output: bounded(request, "max_output_bytes", 262144, 16777216)? as usize,
            max_stored: bounded(request, "max_stored_bytes", 10485760, 104857600)? as usize,
        };
        alive(control, budget.deadline)?;
        control.approve(json!({"kind":"alpha","mode":mode,"start_url":url,"origin":parsed.origin().ascii_serialization(),"provider":provider.name(),"operations":["navigate","click","fill","select","scroll","wait"]}))?;
        alive(control, budget.deadline)?;
        let session = if matches!(provider, Provider::Fixture) {
            None
        } else {
            Some(provider.session()?)
        };
        alive(control, budget.deadline)?;
        let run = run_task_scenario(
            url,
            budget.deadline.saturating_duration_since(Instant::now()),
            &|| alive(control, budget.deadline),
            |page: &mut MatrixPage| {
                let mut history = Vec::<String>::new();
                loop {
                    alive(control, budget.deadline)?;
                    let snapshot = page.snapshot()?;
                    private_text(&snapshot, &blocked)?;
                    outcome.last_url = snapshot["url"].as_str().map(str::to_owned);
                    if checks(page, requested)? {
                        let report = serde_json::to_vec(&json!({"checks_passed":requested.len()}))?;
                        if report.len() > budget.max_stored {
                            return reject("storage_limit");
                        }
                        outcome
                            .artifacts
                            .push(("application/json".to_owned(), report));
                        if request["visual"] == true {
                            outcome.visual_diagnostic = "incomplete".to_owned();
                            outcome.evidence.push("visual:unsafe_capture".to_owned());
                            let defaults = json!([{"width":390,"height":844},{"width":768,"height":1024},{"width":1440,"height":900}]);
                            let viewports = request
                                .get("viewports")
                                .unwrap_or(&defaults)
                                .as_array()
                                .filter(|v| !v.is_empty() && v.len() <= 3)
                                .ok_or("invalid_request")?;
                            for viewport in viewports {
                                alive(control, budget.deadline)?;
                                let geometry = page.inspect_geometry(
                                    viewport["width"].as_u64().ok_or("invalid_request")?,
                                    viewport["height"].as_u64().ok_or("invalid_request")?,
                                )?;
                                let bytes = serde_json::to_vec(&geometry)?;
                                let stored = outcome
                                    .artifacts
                                    .iter()
                                    .map(|(_, b)| b.len())
                                    .sum::<usize>();
                                if stored + bytes.len() > budget.max_stored {
                                    break;
                                }
                                outcome
                                    .artifacts
                                    .push(("application/json".to_owned(), bytes));
                            }
                        }
                        outcome.functional = "verified".to_owned();
                        outcome
                            .evidence
                            .push("All independent completion checks passed.".to_owned());
                        return Ok(());
                    }
                    if budget.steps >= budget.max_steps || budget.calls >= budget.max_calls {
                        return reject("budget_limit");
                    }
                    let offered = actions(&snapshot, &Value::Object(values.clone()))?;
                    let labels = offered
                        .iter()
                        .map(|a| a.description.clone())
                        .collect::<Vec<_>>();
                    private_text(&json!(labels), &blocked)?;
                    let state = json!({"page":{"url":snapshot["url"],"text":snapshot["text"]},"recent_actions":history.iter().rev().take(8).collect::<Vec<_>>()});
                    let selected = if let Some(session) = &session {
                        budget.calls += 1;
                        let answer = session.decide_bounded(
                            goal,
                            &state,
                            &labels,
                            budget.max_input.saturating_sub(budget.input_bytes),
                            budget.max_output.saturating_sub(budget.output_bytes),
                        )?;
                        budget.input_bytes += answer.request_bytes;
                        budget.output_bytes += answer.response_bytes;
                        offered.get(answer.choice).ok_or("invalid_choice")?
                    } else {
                        offered
                            .iter()
                            .find(|a| a.operation != "wait")
                            .ok_or("no_choices")?
                    };
                    alive(control, budget.deadline)?;
                    if deferred(selected) {
                        return reject("deferred_consequence");
                    }
                    let choice = snapshot["choices"].as_array().and_then(|choices| {
                        choices
                            .iter()
                            .find(|choice| choice["id"] == selected.target)
                    });
                    let mut proposal = json!({"kind":"beta","url":snapshot["url"],"operation":selected.operation,"target":selected.target,"description":selected.description});
                    if let Some(choice) = choice {
                        let effect = &choice["effect"];
                        let destination = effect["href"]
                            .as_str()
                            .or_else(|| effect["destination"][0].as_str());
                        let semantics = json!([
                            choice["label"],
                            choice["operation"],
                            choice["tag"],
                            choice["inputType"],
                            effect
                        ]);
                        proposal["semantic_fingerprint"] =
                            json!(crate::sha256_hex(&serde_json::to_vec(&semantics)?));
                        if let Some(destination) = destination {
                            proposal["destination_url"] = json!(destination);
                        }
                    }
                    control.approve(proposal)?;
                    alive(control, budget.deadline)?;
                    budget.steps += 1;
                    if let Err(error) = page.act_guarded(
                        &selected.operation,
                        &selected.target,
                        selected.value.as_deref(),
                        &|| alive(control, budget.deadline),
                    ) {
                        if matches!(selected.operation.as_str(), "scroll" | "wait") {
                            return Err(error);
                        }
                        return reject("action_outcome_unknown");
                    }
                    history.push(selected.description.clone());
                }
            },
        );
        counters = Some(budget);
        run
    })();
    if let Some(budget) = counters {
        outcome.usage = json!({"calls":budget.calls,"steps":budget.steps,"input_bytes":budget.input_bytes,"output_bytes":budget.output_bytes,"stored_bytes":outcome.artifacts.iter().map(|(_,b)|b.len()).sum::<usize>()});
    }
    match result {
        Ok(()) => outcome.terminal = "verified".to_owned(),
        Err(error) => {
            let text = error.to_string();
            let reason = match text.as_str() {
                "cancelled" => "cancelled",
                "budget_limit" | "provider_input_budget_exhausted" => "budget_limit",
                "privacy_blocked" => "privacy_blocked",
                "deferred_consequence" => "deferred_consequence",
                "unsupported_mode" => "unsupported_mode",
                "unsupported_origin" => "unsupported_origin",
                "visual_unsupported" => "visual_unsupported",
                "invalid_request" => "invalid_request",
                "declined" => "declined",
                "admission_declined" => "admission_declined",
                "admission_expired" => "admission_expired",
                "no_supported_human_grant_channel" => "no_supported_human_grant_channel",
                "action_outcome_unknown" => "action_outcome_unknown",
                "output_limit" | "provider_output_budget_exhausted" => "output_limit",
                "task_scope_expansion_required" => "task_scope_expansion_required",
                "storage_limit" => "storage_limit",
                _ => "execution_failed",
            };
            let cleanup_unknown = text == "cleanup_unknown"
                || text.contains("cleanup failed")
                || text.contains("process group is still present");
            let blocked = matches!(
                reason,
                "task_scope_expansion_required"
                    | "declined"
                    | "admission_declined"
                    | "admission_expired"
                    | "no_supported_human_grant_channel"
                    | "unsupported_mode"
                    | "unsupported_origin"
            );
            outcome.terminal = if cleanup_unknown || reason == "action_outcome_unknown" {
                "outcome_unknown"
            } else if blocked {
                "blocked"
            } else if reason == "cancelled" {
                "cancelled"
            } else if ["budget_limit", "output_limit", "storage_limit"].contains(&reason) {
                "limit_exceeded"
            } else {
                "failed"
            }
            .to_owned();
            if !blocked {
                outcome.functional = "failed".to_owned();
            }
            outcome.artifacts.clear();
            outcome.reason = Some(
                if cleanup_unknown {
                    "cleanup_unknown"
                } else {
                    reason
                }
                .to_owned(),
            );
        }
    }
    outcome.timings = json!({"total":started.elapsed().as_secs_f64()});
    outcome
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    struct Control {
        cancelled: bool,
        calls: AtomicUsize,
    }
    impl RunControl for Control {
        fn cancelled(&self) -> bool {
            self.cancelled
        }
        fn approve(&self, _: Value) -> Result<(), &'static str> {
            self.calls.fetch_add(1, Ordering::SeqCst);
            Err("declined")
        }
    }
    fn request() -> Value {
        json!({"goal":"Increment counter","start_url":"http://127.0.0.1:1/","mode":"isolated","checks":[],"values":{},"privacy":{},"limits":{}})
    }
    #[test]
    fn cancellation_prevents_approval_and_browser() {
        let c = Control {
            cancelled: true,
            calls: AtomicUsize::new(0),
        };
        assert_eq!(
            execute(&request(), Provider::Fixture, &c).terminal,
            "cancelled"
        );
        assert_eq!(c.calls.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn declined_scope_prevents_browser() {
        let c = Control {
            cancelled: false,
            calls: AtomicUsize::new(0),
        };
        assert_eq!(
            execute(&request(), Provider::Fixture, &c).reason.as_deref(),
            Some("declined")
        );
        assert_eq!(c.calls.load(Ordering::SeqCst), 1);
    }
    #[test]
    fn unsupported_attachment_and_privacy_fail_before_approval() {
        let c = Control {
            cancelled: false,
            calls: AtomicUsize::new(0),
        };
        let mut r = request();
        r["mode"] = json!("unsupported");
        assert_eq!(
            execute(&r, Provider::Fixture, &c).reason.as_deref(),
            Some("unsupported_mode")
        );
        r["mode"] = json!("isolated");
        r["start_url"] = json!("https://example.org/");
        assert_eq!(
            execute(&r, Provider::Fixture, &c).reason.as_deref(),
            Some("unsupported_origin")
        );
        r["start_url"] = request()["start_url"].clone();
        r["privacy"] = json!({"blocked_literals":["Increment"]});
        assert_eq!(
            execute(&r, Provider::Fixture, &c).reason.as_deref(),
            Some("privacy_blocked")
        );
        assert_eq!(c.calls.load(Ordering::SeqCst), 0);
    }
    #[test]
    fn blocked_literals_match_unescaped_strings_and_keys() {
        let blocked = vec!["a\"b".to_owned()];
        assert!(private_text(&json!({"text":"a\"b"}), &blocked).is_err());
        assert!(private_text(&json!({"a\"b":"value"}), &blocked).is_err());
        assert!(private_text(&json!(["ordinary"]), &blocked).is_ok());
    }
    struct Admitted {
        allow_actions: bool,
        proposals: AtomicUsize,
    }
    impl RunControl for Admitted {
        fn cancelled(&self) -> bool {
            false
        }
        fn approve(&self, proposal: Value) -> Result<(), &'static str> {
            self.proposals.fetch_add(1, Ordering::SeqCst);
            if proposal["kind"] == "alpha" || self.allow_actions {
                Ok(())
            } else {
                Err("declined")
            }
        }
    }
    fn fixture_request(fixture: &crate::matrix_fixtures::Fixture) -> Value {
        let checks = fixture.checks.as_array().unwrap().iter().map(|check| {
            let kind=match check["type"].as_str().unwrap() { "url_equals"=>"route", "text_equals"=>"text", "value_equals"=>"value", _=>panic!("unexpected fixture check") };
            json!({"kind":kind,"target":check["target"].as_str().unwrap_or(""),"expected":check["value"]})
        }).collect::<Vec<_>>();
        json!({"goal":fixture.goal,"start_url":fixture.url,"mode":"isolated","values":fixture.values,"checks":checks,"privacy":{},"limits":{"max_steps":8,"max_provider_calls":8,"max_seconds":30.0}})
    }
    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn native_task_checks_approval_and_independent_result() {
        let fixture = crate::matrix_fixtures::start("click").unwrap();
        let control = Admitted {
            allow_actions: true,
            proposals: AtomicUsize::new(0),
        };
        let request = fixture_request(&fixture);
        let result = execute(&request, Provider::Fixture, &control);
        assert_eq!(result.terminal, "verified", "{:?}", result.reason);
        assert!(fixture.verify().unwrap());
        assert!(control.proposals.load(Ordering::SeqCst) >= 2);
        assert_eq!(result.artifacts.len(), 1);
        assert_eq!(result.artifacts[0].0, "application/json");
        assert_eq!(
            serde_json::from_slice::<Value>(&result.artifacts[0].1).unwrap(),
            json!({"checks_passed":request["checks"].as_array().unwrap().len()})
        );
    }
    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn declined_action_leaves_fixture_untouched() {
        let fixture = crate::matrix_fixtures::start("click").unwrap();
        let control = Admitted {
            allow_actions: false,
            proposals: AtomicUsize::new(0),
        };
        let result = execute(&fixture_request(&fixture), Provider::Fixture, &control);
        assert_ne!(result.terminal, "verified");
        assert!(!fixture.verify().unwrap());
        assert!(result.artifacts.is_empty());
    }
    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn requested_visual_diagnostics_remain_local_and_incomplete() {
        let fixture = crate::matrix_fixtures::start("click").unwrap();
        let control = Admitted {
            allow_actions: true,
            proposals: AtomicUsize::new(0),
        };
        let mut request = fixture_request(&fixture);
        request["visual"] = json!(true);
        request["viewports"] = json!([{"width":390,"height":844}]);
        let result = execute(&request, Provider::Fixture, &control);
        assert_eq!(result.terminal, "verified", "{:?}", result.reason);
        assert_eq!(result.visual_diagnostic, "incomplete");
        assert_eq!(result.artifacts.len(), 2);
        assert_eq!(result.artifacts[1].0, "application/json");
        let geometry: Value = serde_json::from_slice(&result.artifacts[1].1).unwrap();
        assert_eq!(geometry["viewport"], json!({"width":390,"height":844}));
        assert!(
            result
                .evidence
                .iter()
                .any(|item| item == "visual:unsafe_capture")
        );
    }
}
