use decision_browser_rust_cdp::{browser, matrix_fixtures, provider::Provider};
use serde_json::{Value, json};
use std::error::Error;
use std::fs::OpenOptions;
use std::io::{BufWriter, Write};
use std::time::{Duration, Instant};

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;

#[derive(Debug)]
struct Action {
    operation: String,
    target: String,
    caption: String,
    value: Option<String>,
}

fn string(value: &Value, key: &str) -> String {
    value[key].as_str().unwrap_or_default().to_owned()
}

fn possible_actions(page: &Value, values: &Value) -> Result<Vec<Action>> {
    let snapshot = page["choices"]
        .as_array()
        .ok_or("missing browser choices")?;
    let values = values.as_object().ok_or("invalid fixture values")?;
    let mut actions = Vec::new();
    for target in snapshot {
        let id = string(target, "id");
        let op = string(target, "operation");
        let label = string(target, "label");
        let current = string(target, "current");
        match op.as_str() {
            "fill" => {
                let selected = values.get(&label).or_else(|| {
                    values.iter().find_map(|(name, value)| {
                        name.eq_ignore_ascii_case(label.trim()).then_some(value)
                    })
                });
                if let Some(value) = selected.and_then(Value::as_str).filter(|v| *v != current) {
                    actions.push(Action {
                        operation: op,
                        target: id,
                        caption: format!("Fill {label:?} with provided value {value:?}"),
                        value: Some(value.to_owned()),
                    });
                }
            }
            "select" => {
                for option in target["options"].as_array().into_iter().flatten() {
                    let value = string(option, "value");
                    if value.is_empty() || value == current || option["disabled"] == true {
                        continue;
                    }
                    let text = string(option, "label");
                    actions.push(Action {
                        operation: op.clone(),
                        target: id.clone(),
                        caption: format!("Set {label:?} to {text:?}"),
                        value: Some(value),
                    });
                }
            }
            "click" | "enter" => actions.push(Action {
                operation: op.clone(),
                target: id,
                caption: format!(
                    "{op} {label:?}{}",
                    if current.is_empty() {
                        String::new()
                    } else {
                        format!(" (current {current:?})")
                    }
                ),
                value: None,
            }),
            _ => {}
        }
    }
    if actions.iter().any(|action| action.operation == "fill") {
        actions.retain(|action| action.operation == "fill");
        actions.push(Action {
            operation: "wait".to_owned(),
            target: String::new(),
            caption: "Wait briefly if the visible input is still changing".to_owned(),
            value: None,
        });
        return Ok(actions);
    }
    for (operation, caption) in [
        ("scroll_down", "Scroll down to reveal additional content"),
        ("scroll_up", "Scroll up to reveal preceding content"),
        ("wait", "Wait briefly for a pending reveal or navigation"),
    ] {
        if (operation == "scroll_down" && page["scroll"]["down"] != true)
            || (operation == "scroll_up" && page["scroll"]["up"] != true)
        {
            continue;
        }
        actions.push(Action {
            operation: operation.to_owned(),
            target: String::new(),
            caption: caption.to_owned(),
            value: None,
        });
    }
    Ok(actions)
}

#[derive(Default)]
struct Metrics {
    api_ms: u64,
    observe_ms: u64,
    action_ms: u64,
    verify_ms: u64,
    calls: usize,
    input_bytes: usize,
    input_tokens: u64,
    output_tokens: u64,
    model: String,
    actions: Vec<String>,
    checks: Vec<bool>,
    oracle: bool,
}

fn milliseconds(start: Instant) -> u64 {
    start.elapsed().as_millis().try_into().unwrap_or(u64::MAX)
}

fn run_case(provider: Provider, name: &str) -> Value {
    let started = Instant::now();
    let mut stats = Metrics::default();
    let result = (|| -> Result<()> {
        let fixture = matrix_fixtures::start(name)?;
        let checks = fixture.checks.as_array().ok_or("invalid case checks")?;
        let session = provider.session()?;
        let mut history: Vec<String> = Vec::new();
        browser::run_scenario(
            &fixture.url,
            Duration::from_secs(fixture.timeout_seconds),
            |page| {
                for step in 0..=fixture.max_steps {
                    let verification = Instant::now();
                    stats.checks = checks
                        .iter()
                        .map(|check| page.read_check(check))
                        .collect::<Result<Vec<_>>>()?;
                    stats.oracle = fixture.verify()?;
                    stats.verify_ms += milliseconds(verification);
                    if stats.checks.iter().all(|check| *check) && stats.oracle {
                        return Ok(());
                    }
                    if step == fixture.max_steps || stats.calls >= fixture.max_calls {
                        return Err("case reached its step or API-call limit".into());
                    }
                    let observation = Instant::now();
                    let snapshot = page.snapshot()?;
                    let actions = possible_actions(&snapshot, &fixture.values)?;
                    if actions.len() == 1 && actions[0].operation == "wait" {
                        stats.observe_ms += milliseconds(observation);
                        let wait = Instant::now();
                        page.act("wait", "", None)?;
                        stats.action_ms += milliseconds(wait);
                        stats
                            .actions
                            .push("Wait for actionable controls".to_owned());
                        continue;
                    }
                    let labels = actions
                        .iter()
                        .map(|action| action.caption.clone())
                        .collect::<Vec<_>>();
                    let text = string(&snapshot, "text");
                    let excerpt: String = text.chars().take(800).collect();
                    let previous = history
                        .iter()
                        .rev()
                        .filter(|old| **old != excerpt)
                        .take(6)
                        .collect::<Vec<_>>();
                    let state = json!({
                        "page_text": text, "earlier_page_observations": previous,
                        "provided_values": fixture.values, "actions_already_taken": stats.actions,
                        "instruction": "Use visible controls and prior observations to move towards the goal. Choose a visible action before waiting unless the page is still changing. Avoid repeating an action without changed evidence."
                    });
                    if history.last() != Some(&excerpt) {
                        history.push(excerpt);
                    }
                    stats.observe_ms += milliseconds(observation);
                    let api = Instant::now();
                    stats.calls += 1;
                    let decision = session.decide(
                        &fixture.goal,
                        &state,
                        &labels,
                        fixture.max_input_bytes.saturating_sub(stats.input_bytes),
                    );
                    stats.api_ms += milliseconds(api);
                    let decision = decision?;
                    stats.input_bytes += decision.request_bytes;
                    stats.input_tokens += decision
                        .input_tokens
                        .ok_or("provider did not report input tokens")?;
                    stats.output_tokens += decision
                        .output_tokens
                        .ok_or("provider did not report output tokens")?;
                    stats.model = decision.model;
                    let selected = actions
                        .get(decision.choice)
                        .ok_or("decision was outside the offered actions")?;
                    let action = Instant::now();
                    let (operation, value) = match selected.operation.as_str() {
                        "scroll_down" => ("scroll", Some("down")),
                        "scroll_up" => ("scroll", Some("up")),
                        operation => (operation, selected.value.as_deref()),
                    };
                    page.act(operation, &selected.target, value)?;
                    stats.action_ms += milliseconds(action);
                    stats.actions.push(selected.caption.clone());
                }
                Err("case exhausted".into())
            },
        )?;
        Ok(())
    })();
    let elapsed_ms = milliseconds(started);
    json!({
        "case": name, "provider": provider.name(), "model": stats.model,
        "confirmed": result.is_ok(), "error": result.err().map(|error| error.to_string()),
        "elapsed_ms": elapsed_ms, "api_ms": stats.api_ms, "observe_ms": stats.observe_ms,
        "action_ms": stats.action_ms, "verify_ms": stats.verify_ms,
        "calls": stats.calls, "input_bytes": stats.input_bytes, "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens, "checks": stats.checks, "oracle": stats.oracle,
        "actions": stats.actions
    })
}

fn main() -> Result<()> {
    let args = std::env::args().skip(1).collect::<Vec<_>>();
    let names = matrix_fixtures::names();
    if args == ["--list"] {
        println!("{}", serde_json::to_string_pretty(&names)?);
        return Ok(());
    }
    let [provider, output, cases @ ..] = args.as_slice() else {
        return Err("usage: browser_matrix jev|luna|gemini OUTPUT.jsonl [CASE ...]".into());
    };
    let provider = Provider::from_name(provider)?;
    if matches!(provider, Provider::Fixture) || cases.iter().any(|name| !names.contains(name)) {
        return Err("unknown live provider or case".into());
    }
    let cases = if cases.is_empty() { &names[..] } else { cases };
    let mut output = BufWriter::new(
        OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(output)?,
    );
    let mut failed = false;
    for name in cases {
        let report = run_case(provider, name);
        println!(
            "{}",
            json!({"case":name, "provider":provider.name(), "confirmed":report["confirmed"], "elapsed_ms":report["elapsed_ms"], "error":report["error"]})
        );
        serde_json::to_writer(&mut output, &report)?;
        output.write_all(b"\n")?;
        output.flush()?;
        failed |= report["confirmed"] != true;
    }
    if failed {
        return Err("one or more browser scenarios were not confirmed".into());
    }
    Ok(())
}
