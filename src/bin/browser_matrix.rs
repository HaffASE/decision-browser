use decision_browser_rust_cdp::{browser, matrix_fixtures, provider::Provider};
use serde_json::{Value, json};
use std::collections::BTreeSet;
use std::error::Error;
use std::fs::OpenOptions;
use std::io::{BufWriter, Write};
use std::time::{Duration, Instant};

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;

#[derive(Clone, Debug)]
struct Action {
    operation: String,
    target: String,
    caption: String,
    value: Option<String>,
}

#[derive(Default)]
struct ProgressGuard {
    attempted: BTreeSet<(String, String)>,
}

impl ProgressGuard {
    fn state(page: &Value) -> String {
        // Coordinates and node handles do not describe page progress. Keep the
        // visible values, destinations, options and viewport in the comparison.
        let mut stable = page.clone();
        if let Some(choices) = stable["choices"].as_array_mut() {
            for choice in choices {
                if let Some(choice) = choice.as_object_mut() {
                    choice.remove("id");
                }
            }
        }
        stable.to_string()
    }

    fn action(action: &Action) -> String {
        json!([
            action.operation,
            action.target,
            action.caption,
            action.value
        ])
        .to_string()
    }

    fn offer(&self, state: &str, actions: Vec<Action>) -> Vec<Action> {
        let untried: Vec<Action> = actions
            .iter()
            .filter(|action| {
                !self
                    .attempted
                    .contains(&(state.to_owned(), Self::action(action)))
            })
            .cloned()
            .collect();
        if !untried.is_empty() {
            return untried;
        }
        if actions.iter().any(|action| action.operation != "wait") {
            return actions
                .into_iter()
                .filter(|action| action.operation != "wait")
                .collect();
        }
        // A page with no actionable controls can legitimately need another wait.
        actions
    }

    fn record(&mut self, state: &str, action: &Action) {
        self.attempted
            .insert((state.to_owned(), Self::action(action)));
    }
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
        let mut progress = ProgressGuard::default();
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
                    let state_key = ProgressGuard::state(&snapshot);
                    let actions =
                        progress.offer(&state_key, possible_actions(&snapshot, &fixture.values)?);
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
                    let selected = if let [only] = actions.as_slice() {
                        // A single fixture action needs no comparative decision.
                        only
                    } else {
                        let labels = actions
                            .iter()
                            .map(|action| action.caption.clone())
                            .collect::<Vec<_>>();
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
                        actions
                            .get(decision.choice)
                            .ok_or("decision was outside the offered actions")?
                    };
                    let action = Instant::now();
                    let (operation, value) = match selected.operation.as_str() {
                        "scroll_down" => ("scroll", Some("down")),
                        "scroll_up" => ("scroll", Some("up")),
                        operation => (operation, selected.value.as_deref()),
                    };
                    page.act(operation, &selected.target, value)?;
                    progress.record(&state_key, selected);
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

#[cfg(test)]
mod tests {
    use super::*;

    fn action(operation: &str, value: &str) -> Action {
        Action {
            operation: operation.to_owned(),
            target: "field".to_owned(),
            caption: value.to_owned(),
            value: Some(value.to_owned()),
        }
    }

    #[test]
    fn unchanged_input_cannot_repeat_wait_while_fill_is_available() {
        let page = json!({"url":"http://localhost/search", "text":"Search", "choices":[{"id":"e1","current":"","operation":"fill","label":"Query"}]});
        let state = ProgressGuard::state(&page);
        let wait = action("wait", "Wait");
        let fill = action("fill", "Godel");
        let mut guard = ProgressGuard::default();
        guard.record(&state, &wait);
        let offered = guard.offer(&state, vec![fill, wait]);
        assert_eq!(offered.len(), 1);
        assert_eq!(offered[0].operation, "fill");
    }

    #[test]
    fn returning_to_the_same_selection_excludes_the_attempted_cycle() {
        let platform2 = "platform 2";
        let platform4 = "platform 4";
        let to4 = action("select", "Platform 4");
        let to2 = action("select", "Platform 2");
        let submit = action("click", "Add platform");
        let mut guard = ProgressGuard::default();
        guard.record(platform2, &to4);
        guard.record(platform4, &to2);
        let offered = guard.offer(platform2, vec![to4, submit]);
        assert_eq!(offered.len(), 1);
        assert_eq!(offered[0].operation, "click");
    }

    #[test]
    fn changed_evidence_restores_the_same_action() {
        let click = action("click", "Increase");
        let mut guard = ProgressGuard::default();
        guard.record("count=0", &click);
        assert_eq!(
            guard
                .offer("count=1", vec![click, action("wait", "Wait")])
                .len(),
            2
        );
    }

    #[test]
    fn loading_without_controls_preserves_repeated_waits() {
        let wait = action("wait", "Wait");
        let mut guard = ProgressGuard::default();
        guard.record("Loading", &wait);
        assert_eq!(guard.offer("Loading", vec![wait])[0].operation, "wait");
    }

    #[test]
    fn node_handle_churn_is_not_page_progress() {
        let mut page = json!({"url":"http://localhost", "choices":[{"id":"e1","current":"","operation":"fill","label":"Query"}]});
        let before = ProgressGuard::state(&page);
        page["choices"][0]["id"] = json!("e5");
        assert_eq!(before, ProgressGuard::state(&page));
        page["choices"][0]["current"] = json!("ready");
        assert_ne!(before, ProgressGuard::state(&page));
    }
}
