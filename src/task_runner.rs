use crate::browser::{BrowserResult, MatrixPage, run_existing_task_scenario, run_task_scenario};
use crate::native_policy::{Classification, classify, validate_url};
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

fn actions(snapshot: &Value, values: &Value, helper_available: bool) -> BrowserResult<Vec<Action>> {
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
                    });
                } else if provided.is_none() && current.is_empty() && helper_available {
                    actions.push(Action {
                        operation: operation.to_owned(),
                        target: target.to_owned(),
                        value: None,
                        description: format!("Fill {label:?} with a value generated for the task"),
                    });
                }
                if choice["effect"]["destination"].is_array()
                    && !current.is_empty()
                    && choice["tag"] == "INPUT"
                    && !actions
                        .iter()
                        .any(|action| action.target == target && action.operation == "fill")
                {
                    actions.push(Action {
                        operation: "enter".to_owned(),
                        target: target.to_owned(),
                        value: None,
                        description: format!("Press Enter in {label:?}"),
                    });
                }
            }
            "select" => {
                let selected_label = choice["options"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .find(|option| option["value"] == current)
                    .and_then(|option| option["label"].as_str())
                    .unwrap_or("");
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
                        description: format!(
                            "Select {:?} for {label:?}; selected {selected_label:?}",
                            option["label"]
                        ),
                    });
                }
            }
            "click" => actions.push(Action {
                operation: if choice["effect"]["destination"].is_array()
                    && matches!(choice["inputType"].as_str(), Some("submit" | "image"))
                {
                    "submit"
                } else {
                    operation
                }
                .to_owned(),
                target: target.to_owned(),
                value: None,
                description: {
                    let path = choice["effect"]["href"]
                        .as_str()
                        .and_then(|url| validate_url(url).ok())
                        .map(|url| url.path().to_owned());
                    let state = match choice["inputType"].as_str() {
                        Some("checkbox" | "radio") => format!("; checked {}", current == "true"),
                        _ => String::new(),
                    };
                    format!(
                        "Click {label:?}{state}{}",
                        path.map(|path| format!("; path {path:?}"))
                            .unwrap_or_default()
                    )
                },
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
            });
        }
    }
    actions.push(Action {
        operation: "wait".to_owned(),
        target: String::new(),
        value: None,
        description: "Wait for a changing page".to_owned(),
    });
    for (operation, description) in [
        ("done", "DONE — the requested goal is complete"),
        (
            "unable",
            "UNABLE — the requested goal cannot be completed with available controls",
        ),
    ] {
        actions.push(Action {
            operation: operation.to_owned(),
            target: String::new(),
            value: None,
            description: description.to_owned(),
        });
    }
    Ok(actions)
}

fn batch(actions: Vec<Action>, offset: usize, maximum: usize) -> Vec<Action> {
    if actions.len() <= maximum {
        return actions;
    }
    let (stops, controls): (Vec<_>, Vec<_>) = actions
        .into_iter()
        .partition(|action| maximum >= 4 && matches!(action.operation.as_str(), "done" | "unable"));
    let width = maximum.saturating_sub(stops.len() + 1).max(1);
    let start = offset % controls.len();
    let mut offered = controls
        .iter()
        .skip(start)
        .take(width)
        .cloned()
        .collect::<Vec<_>>();
    offered.push(Action {
        operation: "more_controls".to_owned(),
        target: String::new(),
        value: None,
        description: format!(
            "Show the next group of controls on this page ({} total)",
            controls.len()
        ),
    });
    offered.extend(stops);
    offered
}

fn proposal(snapshot: &Value, action: &Action) -> BrowserResult<Value> {
    let document_url = snapshot["url"].as_str().ok_or("observation_failed")?;
    let document = validate_url(document_url)?;
    let choice = snapshot["choices"]
        .as_array()
        .and_then(|choices| choices.iter().find(|choice| choice["id"] == action.target));
    let mut signals = Vec::new();
    let mut destination = document_url;
    let mut semantics = json!([action.operation, action.target]);
    if let Some(choice) = choice {
        signals.push(choice["label"].as_str().unwrap_or_default().to_owned());
        let effect = &choice["effect"];
        if effect["download"] == true {
            return reject("unsupported_operation");
        }
        if effect["sensitiveForm"] == true {
            return reject("privacy_blocked");
        }
        signals.extend(
            effect["signals"]
                .as_array()
                .into_iter()
                .flatten()
                .filter_map(Value::as_str)
                .map(str::to_owned),
        );
        destination = effect["href"]
            .as_str()
            .or_else(|| effect["destination"][0].as_str())
            .unwrap_or(document_url);
        semantics = json!([
            choice["label"],
            choice["operation"],
            choice["tag"],
            choice["inputType"],
            effect
        ]);
    }
    let destination = validate_url(destination)?;
    let opaque = action.operation == "click"
        && choice.is_some_and(|choice| {
            choice["effect"]["href"].is_null()
                && !matches!(choice["inputType"].as_str(), Some("checkbox" | "radio"))
        });
    let classification = classify(destination.as_str(), &signals, opaque);
    match classification {
        Classification::Consequential => return reject("consequential_action_blocked"),
        Classification::Unresolved => {}
        Classification::Ordinary => {}
    }
    let origin = document.origin().ascii_serialization();
    let destination_origin = destination.origin().ascii_serialization();
    if origin != destination_origin {
        return reject("task_scope_expansion_required");
    }
    Ok(
        json!({"kind":"beta","url":document_url,"document_origin":origin,
        "destination_origin":destination_origin,"destination_url":destination.as_str(),
        "operation":action.operation,"target":action.target,"description":action.description,
        "classification":if classification==Classification::Ordinary {"ordinary"}else{"unresolved"},
        "residual_risk":if opaque {"The control invokes page-defined behavior. Its exact browser-side effect is unknown."}else{"The page can run its own handlers during this native interaction."},
        "semantic_fingerprint":crate::sha256_hex(&serde_json::to_vec(&json!([semantics, action.value]))?)}),
    )
}

fn checks(page: &mut MatrixPage, requested: &[Value]) -> BrowserResult<bool> {
    page.read_public_checks(requested)
}

fn finish(outcome: &mut TaskOutcome, has_checks: bool, unable: bool) {
    outcome.terminal = if has_checks {
        "verification_failed"
    } else if unable {
        "blocked"
    } else {
        "completed_unverified"
    }
    .to_owned();
    outcome.functional = if has_checks { "failed" } else { "unverified" }.to_owned();
    outcome.reason = Some(
        if has_checks {
            "verification_failed"
        } else if unable {
            "model_unable"
        } else {
            "completed_unverified"
        }
        .to_owned(),
    );
}

pub fn execute(request: &Value, provider: Provider, control: &dyn RunControl) -> TaskOutcome {
    let mut normalized = request.clone();
    if let Some(fields) = normalized.as_object_mut() {
        for (field, default) in [
            ("mode", json!("isolated")),
            ("checks", json!([])),
            ("values", json!({})),
            ("privacy", json!({})),
            ("limits", json!({})),
        ] {
            fields.entry(field.to_owned()).or_insert(default);
        }
        if let Some(goal) = fields.get("goal").and_then(Value::as_str).map(str::trim) {
            fields.insert("goal".to_owned(), json!(goal));
        }
    }
    let request = &normalized;
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
    let mut unobserved_input = false;
    let result = (|| -> BrowserResult<()> {
        let mode = request["mode"].as_str().unwrap_or("isolated");
        if !matches!(mode, "isolated" | "existing_chrome") {
            return reject("unsupported_mode");
        }

        let goal = request["goal"]
            .as_str()
            .filter(|s| !s.trim().is_empty() && s.chars().count() <= 4000)
            .ok_or("invalid_request")?;
        let url = request["start_url"].as_str().ok_or("invalid_request")?;
        let parsed = validate_url(url)?;
        if classify(url, &[], false) == Classification::Consequential {
            return reject("consequential_action_blocked");
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
        control.approve(json!({
            "kind": "alpha",
            "mode": mode,
            "goal": goal,
            "start_url": url,
            "origin": parsed.origin().ascii_serialization(),
            "provider": provider.name(),
            "operations": ["navigate", "click", "fill", "select", "enter", "submit", "scroll", "wait"],
            "budgets": {
                "max_seconds": seconds,
                "max_steps": budget.max_steps,
                "max_provider_calls": budget.max_calls,
                "max_input_bytes": budget.max_input,
                "max_output_bytes": budget.max_output,
                "max_stored_bytes": budget.max_stored
            }
        }))?;
        alive(control, budget.deadline)?;
        let session = if matches!(provider, Provider::Fixture) {
            None
        } else {
            Some(provider.session()?)
        };
        alive(control, budget.deadline)?;
        let runner = if mode == "existing_chrome" {
            run_existing_task_scenario
        } else {
            run_task_scenario
        };
        let run = runner(
            url,
            budget.deadline.saturating_duration_since(Instant::now()),
            &|| alive(control, budget.deadline),
            |page: &mut MatrixPage| {
                let mut history = Vec::<String>::new();
                let mut previous_url = parsed.clone();
                let mut approved_destination = (parsed.clone(), false);
                let mut control_offset = 0usize;
                let mut control_state = String::new();
                loop {
                    alive(control, budget.deadline)?;
                    let snapshot = page.snapshot()?;
                    if snapshot["oversized"] == true {
                        return reject("observation_limit");
                    }
                    private_text(&snapshot, &blocked)?;
                    let observed =
                        validate_url(snapshot["url"].as_str().ok_or("observation_failed")?)?;
                    if observed != previous_url {
                        let (destination, query_allowed) = &approved_destination;
                        let matches = if *query_allowed {
                            observed.origin() == destination.origin()
                                && observed.path() == destination.path()
                                && observed.fragment() == destination.fragment()
                        } else {
                            observed == *destination
                        };
                        if !matches {
                            return reject("action_outcome_unknown");
                        }
                    }
                    previous_url = observed;
                    outcome.last_url = snapshot["url"].as_str().map(str::to_owned);
                    let verified = checks(page, requested)?;
                    alive(control, budget.deadline)?;
                    unobserved_input = false;
                    if verified {
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
                        alive(control, budget.deadline)?;
                        outcome.functional = "verified".to_owned();
                        alive(control, budget.deadline)?;
                        outcome
                            .evidence
                            .push("All independent completion checks passed.".to_owned());
                        return Ok(());
                    }
                    if budget.steps >= budget.max_steps || budget.calls >= budget.max_calls {
                        return reject("budget_limit");
                    }
                    let available = actions(
                        &snapshot,
                        &Value::Object(values.clone()),
                        session
                            .as_ref()
                            .is_some_and(|session| session.text_helper_available()),
                    )?;
                    let state_key = crate::sha256_hex(&serde_json::to_vec(&json!([
                        snapshot["text"],
                        available
                            .iter()
                            .map(|action| &action.description)
                            .collect::<Vec<_>>()
                    ]))?);
                    if state_key != control_state {
                        control_offset = 0;
                        control_state = state_key;
                    }
                    let offered = batch(
                        available,
                        control_offset,
                        session
                            .as_ref()
                            .map_or(255, |session| session.max_choices()),
                    );
                    let labels = offered
                        .iter()
                        .map(|a| a.description.clone())
                        .collect::<Vec<_>>();
                    private_text(&json!(labels), &blocked)?;
                    let state = json!({"page":{"url":snapshot["url"],"text":snapshot["text"]},"recent_actions":history.iter().rev().take(8).collect::<Vec<_>>()});
                    let mut selected = if let Some(session) = &session {
                        budget.calls += 1;
                        let answer = session.decide_with_timeout(
                            goal,
                            &state,
                            &labels,
                            budget.max_input.saturating_sub(budget.input_bytes),
                            budget.max_output.saturating_sub(budget.output_bytes),
                            budget.deadline.saturating_duration_since(Instant::now()),
                        );
                        alive(control, budget.deadline)?;
                        let answer = answer?;
                        budget.input_bytes += answer.request_bytes;
                        budget.output_bytes += answer.response_bytes;
                        offered.get(answer.choice).ok_or("invalid_choice")?
                    } else {
                        offered
                            .iter()
                            .find(|a| a.operation != "wait")
                            .ok_or("no_choices")?
                    }
                    .clone();
                    alive(control, budget.deadline)?;
                    if selected.operation == "more_controls" {
                        control_offset += offered
                            .iter()
                            .filter(|action| {
                                !matches!(
                                    action.operation.as_str(),
                                    "done" | "unable" | "more_controls"
                                )
                            })
                            .count();
                        budget.steps += 1;
                        continue;
                    }
                    if matches!(selected.operation.as_str(), "done" | "unable") {
                        finish(
                            &mut outcome,
                            !requested.is_empty(),
                            selected.operation == "unable",
                        );
                        return Ok(());
                    }
                    let scope = proposal(&snapshot, &selected)?;
                    private_text(&scope, &blocked)?;
                    if selected.operation == "fill" && selected.value.is_none() {
                        if budget.calls >= budget.max_calls {
                            return reject("budget_limit");
                        }
                        let choice = snapshot["choices"]
                            .as_array()
                            .and_then(|choices| {
                                choices
                                    .iter()
                                    .find(|choice| choice["id"] == selected.target)
                            })
                            .ok_or("observation_failed")?;
                        let session = session.as_ref().ok_or("text_helper_unavailable")?;
                        budget.calls += 1;
                        let generated = session.generate_text_with_timeout(
                            goal,
                            choice["label"].as_str().unwrap_or_default(),
                            snapshot["text"].as_str().unwrap_or_default(),
                            &blocked,
                            (
                                budget.max_input.saturating_sub(budget.input_bytes),
                                budget.max_output.saturating_sub(budget.output_bytes),
                            ),
                            budget.deadline.saturating_duration_since(Instant::now()),
                        );
                        alive(control, budget.deadline)?;
                        let generated = generated?;
                        budget.input_bytes += generated.request_bytes;
                        budget.output_bytes += generated.response_bytes;
                        selected.value = Some(generated.value);
                        alive(control, budget.deadline)?;
                    }
                    control.approve(proposal(&snapshot, &selected)?)?;
                    alive(control, budget.deadline)?;
                    if !matches!(selected.operation.as_str(), "wait" | "scroll") {
                        let scope = proposal(&snapshot, &selected)?;
                        let query_allowed =
                            matches!(selected.operation.as_str(), "submit" | "enter")
                                && snapshot["choices"]
                                    .as_array()
                                    .into_iter()
                                    .flatten()
                                    .find(|choice| choice["id"] == selected.target)
                                    .and_then(|choice| choice["effect"]["destination"][1].as_str())
                                    .is_some_and(|method| method.eq_ignore_ascii_case("get"));
                        approved_destination = (
                            validate_url(
                                scope["destination_url"]
                                    .as_str()
                                    .ok_or("invalid_destination")?,
                            )?,
                            query_allowed,
                        );
                    }
                    budget.steps += 1;
                    unobserved_input = !matches!(selected.operation.as_str(), "wait" | "scroll");
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
        Ok(()) if outcome.functional == "verified" => outcome.terminal = "verified".to_owned(),
        Ok(()) => {}
        Err(error) => {
            let text = error.to_string();
            let reason = match text.as_str() {
                "cancelled" => "cancelled",
                "budget_limit" | "provider_input_budget_exhausted" => "budget_limit",
                "privacy_blocked" => "privacy_blocked",
                "consequential_action_blocked" => "consequential_action_blocked",
                "unresolved_action_scope" => "unresolved_action_scope",
                "unsupported_operation" => "unsupported_operation",
                "observation_limit" => "observation_limit",
                "unsupported_mode" => "unsupported_mode",
                "unsupported_origin" => "unsupported_origin",
                "visual_unsupported" => "visual_unsupported",
                "invalid_request" => "invalid_request",
                "invalid_destination" => "invalid_destination",
                "devtools_port_unreadable" => "devtools_port_unreadable",
                "devtools_port_malformed" => "devtools_port_malformed",
                "endpoint_rejected" => "endpoint_rejected",
                "existing_chrome_unsupported" => "existing_chrome_unsupported",
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
                    | "privacy_blocked"
                    | "consequential_action_blocked"
                    | "unresolved_action_scope"
                    | "unsupported_operation"
                    | "invalid_destination"
                    | "devtools_port_unreadable"
                    | "devtools_port_malformed"
                    | "endpoint_rejected"
                    | "existing_chrome_unsupported"
            );
            outcome.terminal =
                if cleanup_unknown || unobserved_input || reason == "action_outcome_unknown" {
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
                } else if unobserved_input {
                    "action_outcome_unknown"
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
    fn omitted_request_defaults_are_applied_before_admission() {
        let c = Control {
            cancelled: false,
            calls: AtomicUsize::new(0),
        };
        let result = execute(
            &json!({"goal":" Read a page ","start_url":"https://example.org/"}),
            Provider::Fixture,
            &c,
        );
        assert_eq!(result.reason.as_deref(), Some("declined"));
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
        r["start_url"] = json!("https://example.org/checkout");
        assert_eq!(
            execute(&r, Provider::Fixture, &c).reason.as_deref(),
            Some("consequential_action_blocked")
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
    #[test]
    fn finishing_without_checks_never_claims_verified() {
        let c = Control {
            cancelled: true,
            calls: AtomicUsize::new(0),
        };
        let mut result = execute(&request(), Provider::Fixture, &c);
        finish(&mut result, false, false);
        assert_eq!(result.terminal, "completed_unverified");
        assert_eq!(result.functional, "unverified");
        finish(&mut result, false, true);
        assert_eq!(result.terminal, "blocked");
        assert_eq!(result.reason.as_deref(), Some("model_unable"));
        finish(&mut result, true, false);
        assert_eq!(result.terminal, "verification_failed");
        assert_eq!(result.functional, "failed");
    }
    #[test]
    fn ordinary_public_and_existing_modes_reach_explicit_admission() {
        for mode in ["isolated", "existing_chrome"] {
            let c = Control {
                cancelled: false,
                calls: AtomicUsize::new(0),
            };
            let mut r = request();
            r["mode"] = json!(mode);
            r["start_url"] = json!("https://example.org/search");
            let result = execute(&r, Provider::Fixture, &c);
            assert_eq!(result.reason.as_deref(), Some("declined"));
            assert_eq!(c.calls.load(Ordering::SeqCst), 1);
            assert!(result.artifacts.is_empty());
        }
    }
    fn choice_snapshot(effect: Value) -> Value {
        json!({"url":"https://example.org/form","choices":[{"id":"e1","operation":"fill","tag":"INPUT","inputType":"text","label":"Search","current":"orbital","effect":effect}]})
    }
    #[test]
    fn enter_and_submission_scope_binds_destination_and_provided_value() {
        let effect =
            json!({"destination":["https://example.org/find","get",""],"signals":["Search"]});
        let snapshot = choice_snapshot(effect);
        let offered = actions(&snapshot, &json!({"Search":"orbital"}), false).unwrap();
        let enter = offered
            .iter()
            .find(|action| action.operation == "enter")
            .unwrap();
        let first = proposal(&snapshot, enter).unwrap();
        assert_eq!(first["destination_url"], "https://example.org/find");
        let mut different = enter.clone();
        different.value = Some("changed".to_owned());
        assert_ne!(
            proposal(&snapshot, &different).unwrap()["semantic_fingerprint"],
            first["semantic_fingerprint"]
        );
        let changed = choice_snapshot(
            json!({"destination":["https://example.org/publish","post",""],"signals":["Search"]}),
        );
        assert_eq!(
            proposal(&changed, enter).unwrap_err().to_string(),
            "consequential_action_blocked"
        );
        let changed = choice_snapshot(
            json!({"destination":["https://other.example.org/find","get",""],"signals":["Search"]}),
        );
        assert_eq!(
            proposal(&changed, enter).unwrap_err().to_string(),
            "task_scope_expansion_required"
        );
    }
    #[test]
    fn an_empty_unbound_field_requires_an_available_text_helper() {
        let mut snapshot =
            choice_snapshot(json!({"destination":["https://example.org/find","get",""]}));
        snapshot["choices"][0]["current"] = json!("");
        let unavailable = actions(&snapshot, &json!({}), false).unwrap();
        assert!(!unavailable.iter().any(|action| action.operation == "fill"));
        let available = actions(&snapshot, &json!({}), true).unwrap();
        let fill = available
            .iter()
            .find(|action| action.operation == "fill")
            .unwrap();
        assert!(fill.value.is_none());
        assert_eq!(
            fill.description,
            "Fill \"Search\" with a value generated for the task"
        );
    }
    #[test]
    fn large_pages_offer_bounded_groups_and_keep_every_control_reachable() {
        let all = (0..500)
            .map(|index| Action {
                operation: "click".to_owned(),
                target: format!("e{index}"),
                value: None,
                description: format!("Open article {index}"),
            })
            .collect::<Vec<_>>();
        for limit in [2, 26, 255] {
            let mut offset = 0;
            let mut seen = std::collections::BTreeSet::new();
            while seen.len() < all.len() {
                let offered = batch(all.clone(), offset, limit);
                assert!(offered.len() <= limit);
                let count = offered
                    .iter()
                    .filter(|action| action.operation == "click")
                    .count();
                for action in offered.iter().filter(|action| action.operation == "click") {
                    seen.insert(action.target.clone());
                }
                assert!(count > 0);
                offset += count;
            }
            assert_eq!(seen.len(), 500);
        }
    }
    #[test]
    fn opaque_clicks_are_explicitly_unresolved_and_link_paths_disambiguate_labels() {
        let snapshot = json!({"url":"https://example.org/", "choices":[
            {"id":"e1","operation":"click","label":"Open","tag":"BUTTON","inputType":"button","effect":{}},
            {"id":"e2","operation":"click","label":"Open","tag":"A","inputType":"","effect":{"href":"https://example.org/details"}}
        ]});
        let offered = actions(&snapshot, &json!({}), false).unwrap();
        let opaque = proposal(&snapshot, &offered[0]).unwrap();
        assert_eq!(opaque["classification"], "unresolved");
        assert!(
            opaque["residual_risk"]
                .as_str()
                .unwrap()
                .contains("unknown")
        );
        assert!(offered[1].description.contains("/details"));
        assert_eq!(
            proposal(&snapshot, &offered[1]).unwrap()["classification"],
            "ordinary"
        );
    }
    #[test]
    fn private_forms_and_downloads_fail_before_action_admission() {
        let action = Action {
            operation: "submit".to_owned(),
            target: "e1".to_owned(),
            value: None,
            description: "Search".to_owned(),
        };
        let snapshot = choice_snapshot(json!({"sensitiveForm":true}));
        assert_eq!(
            proposal(&snapshot, &action).unwrap_err().to_string(),
            "privacy_blocked"
        );
        let snapshot = choice_snapshot(json!({"download":true}));
        assert_eq!(
            proposal(&snapshot, &action).unwrap_err().to_string(),
            "unsupported_operation"
        );
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
    fn ordinary_native_search_fills_and_submits_through_enter() {
        let fixture = crate::matrix_fixtures::start("search").unwrap();
        let control = Admitted {
            allow_actions: true,
            proposals: AtomicUsize::new(0),
        };
        let result = execute(&fixture_request(&fixture), Provider::Fixture, &control);
        assert_eq!(result.terminal, "verified", "{:?}", result.reason);
        assert!(fixture.verify().unwrap());
        assert_eq!(result.usage["steps"], 2);
        assert_eq!(control.proposals.load(Ordering::SeqCst), 3);
    }
    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn native_counter_form_produces_three_ordinary_submits() {
        struct OrdinaryOnly(AtomicUsize);
        impl RunControl for OrdinaryOnly {
            fn cancelled(&self) -> bool {
                false
            }

            fn approve(&self, scope: Value) -> Result<(), &'static str> {
                if scope["kind"] == "alpha" {
                    return Ok(());
                }
                if scope["operation"] == "submit" && scope["classification"] == "ordinary" {
                    self.0.fetch_add(1, Ordering::SeqCst);
                    Ok(())
                } else {
                    Err("unexpected_counter_action")
                }
            }
        }

        let fixture = crate::matrix_fixtures::start("counter").unwrap();
        let mut request = fixture_request(&fixture);
        let url = json!(format!("{}?control=native", fixture.url));
        request["start_url"] = url.clone();
        request["checks"][1]["expected"] = url;
        request["limits"]["max_steps"] = json!(3);
        let control = OrdinaryOnly(AtomicUsize::new(0));
        let result = execute(&request, Provider::Fixture, &control);
        assert_eq!(result.terminal, "verified", "{:?}", result.reason);
        assert_eq!(control.0.load(Ordering::SeqCst), 3);
        assert_eq!(result.usage["steps"], 3);
        let observed = fixture.oracle().unwrap();
        assert_eq!(observed["counter"], 3);
        assert_eq!(observed["observed"].as_array().unwrap().len(), 3);
    }
    #[test]
    #[ignore = "requires installed sandboxed Chrome"]
    fn a_check_free_native_task_can_finish_without_verification() {
        use std::io::{Read, Write};
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        let worker = std::thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(5);
            while Instant::now() < deadline {
                if let Ok((mut stream, _)) = listener.accept() {
                    stream
                        .set_read_timeout(Some(Duration::from_millis(500)))
                        .unwrap();
                    let mut request = [0; 1024];
                    let _ = stream.read(&mut request);
                    let body = "<!doctype html><h1>Read this heading</h1>";
                    write!(stream, "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
                    break;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
        });
        let control = Admitted {
            allow_actions: true,
            proposals: AtomicUsize::new(0),
        };
        let request = json!({"goal":"Read the heading", "start_url":url});
        let result = execute(&request, Provider::Fixture, &control);
        worker.join().unwrap();
        assert_eq!(
            result.terminal, "completed_unverified",
            "{:?}",
            result.reason
        );
        assert_eq!(result.functional, "unverified");
        assert_eq!(result.usage["steps"], 0);
        assert_eq!(control.proposals.load(Ordering::SeqCst), 1);
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
