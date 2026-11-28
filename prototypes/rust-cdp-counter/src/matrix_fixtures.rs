use serde_json::{Value, json};
use std::error::Error;
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

type Result<T> = std::result::Result<T, Box<dyn Error + Send + Sync>>;
const CASES: &str = include_str!("../fixtures/cases.json");
const WORKFLOWS: &str = include_str!("../fixtures/workflows.html");

pub struct Fixture {
    pub url: String,
    pub goal: String,
    pub values: Value,
    pub checks: Value,
    pub max_steps: usize,
    pub max_calls: usize,
    pub timeout_seconds: u64,
    pub max_input_bytes: usize,
    spec: Value,
    state: Arc<Mutex<State>>,
    stop: Arc<AtomicBool>,
    worker: Option<JoinHandle<()>>,
}

#[derive(Default)]
struct State {
    events: Vec<Value>,
    activated: usize,
    count: usize,
    query: Value,
    order: Value,
    details_visits: usize,
}

fn specs() -> Value {
    serde_json::from_str(CASES).expect("checked-in scenario JSON")
}

pub fn names() -> Vec<String> {
    specs()["cases"]
        .as_array()
        .unwrap()
        .iter()
        .map(|case| case["name"].as_str().unwrap().to_owned())
        .collect()
}

pub fn start(name: &str) -> Result<Fixture> {
    let spec = specs()["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|case| case["name"] == name)
        .cloned()
        .ok_or("unknown matrix case")?;
    let state = Arc::new(Mutex::new(State::default()));
    let stop = Arc::new(AtomicBool::new(false));
    let (url, worker) = if spec["kind"] == "public" {
        ("https://www.python.org/".to_owned(), None)
    } else {
        let listener = TcpListener::bind(("127.0.0.1", 0))?;
        listener.set_nonblocking(true)?;
        let url = format!("http://{}/{name}/", listener.local_addr()?);
        let data = spec.clone();
        let shared = state.clone();
        let stopped = stop.clone();
        let worker = thread::spawn(move || {
            while !stopped.load(Ordering::Acquire) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let _ = serve(stream, &data, &shared);
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(_) => break,
                }
            }
        });
        (url, Some(worker))
    };
    let checks = serde_json::from_str(&spec["checks"].to_string().replace("__BASE_URL__", &url))?;
    Ok(Fixture {
        url,
        goal: spec["goal"].as_str().unwrap().to_owned(),
        values: spec["values"].clone(),
        checks,
        max_steps: spec["max_steps"].as_u64().unwrap() as usize,
        max_calls: spec["max_provider_calls"].as_u64().unwrap() as usize,
        timeout_seconds: spec["timeout_s"].as_f64().unwrap() as u64,
        max_input_bytes: spec["max_input_bytes"].as_u64().unwrap() as usize,
        spec,
        state,
        stop,
        worker,
    })
}

impl Fixture {
    pub fn verify(&self) -> Result<bool> {
        let state = self
            .state
            .lock()
            .map_err(|_| "fixture state lock poisoned")?;
        let name = self.spec["name"].as_str().unwrap();
        if self.spec["kind"] == "public" {
            return Ok(true);
        }
        if self.spec["kind"] == "baseline" {
            return Ok(match name {
                "click" => state.activated >= 1,
                "counter" => state.count == 3,
                "search" => state.query == "orbital mechanics",
                "select" => state.order == "newest",
                "navigate" => state.details_visits >= 1,
                _ => false,
            });
        }
        let expected = self.spec["expected_events"].as_array().unwrap();
        if name == "hotel" {
            if state.events.len() != expected.len() || state.events.last() != expected.last() {
                return Ok(false);
            }
            let mut observed = state.events[..state.events.len() - 1]
                .iter()
                .map(Value::to_string)
                .collect::<Vec<_>>();
            let mut wanted = expected[..expected.len() - 1]
                .iter()
                .map(Value::to_string)
                .collect::<Vec<_>>();
            observed.sort();
            wanted.sort();
            return Ok(observed == wanted);
        }
        Ok(state.events == *expected)
    }

    pub fn oracle(&self) -> Result<Value> {
        let success = self.verify()?;
        let state = self
            .state
            .lock()
            .map_err(|_| "fixture state lock poisoned")?;
        Ok(
            json!({"success":success,"observed":state.events,"expected":self.spec["expected_events"],
            "counter":state.count,"details_visits":state.details_visits}),
        )
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn serve(mut stream: TcpStream, spec: &Value, state: &Mutex<State>) -> Result<()> {
    stream.set_read_timeout(Some(Duration::from_secs(5)))?;
    stream.set_write_timeout(Some(Duration::from_secs(5)))?;
    let mut bytes = Vec::new();
    let mut chunk = [0; 4096];
    let boundary = loop {
        let count = stream.read(&mut chunk)?;
        if count == 0 {
            return Ok(());
        }
        bytes.extend_from_slice(&chunk[..count]);
        if bytes.len() > 16384 {
            return Err("HTTP header too large".into());
        }
        if let Some(index) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
            break index + 4;
        }
    };
    let header = std::str::from_utf8(&bytes[..boundary])?;
    let mut first = header
        .lines()
        .next()
        .ok_or("HTTP request missing")?
        .split_whitespace();
    let method = first.next().unwrap_or("").to_owned();
    let target = first.next().unwrap_or("").to_owned();
    let length = header
        .lines()
        .skip(1)
        .filter_map(|line| line.split_once(':'))
        .find(|(key, _)| key.eq_ignore_ascii_case("content-length"))
        .map(|(_, value)| value.trim().parse::<usize>())
        .transpose()?
        .unwrap_or(0);
    if length > 8192 {
        return reply(&mut stream, 400, "text/plain", "body too large", None);
    }
    while bytes.len() < boundary + length {
        let count = stream.read(&mut chunk)?;
        if count == 0 {
            return Err("incomplete HTTP body".into());
        }
        bytes.extend_from_slice(&chunk[..count]);
    }
    let body = std::str::from_utf8(&bytes[boundary..boundary + length])?;
    let parsed = reqwest::Url::parse(&format!("http://localhost{target}"))?;
    let name = spec["name"].as_str().unwrap();
    let root = format!("/{name}/");
    let Some(suffix) = parsed.path().strip_prefix(&root) else {
        return reply(&mut stream, 404, "text/plain", "not found", None);
    };
    let mut state = state.lock().map_err(|_| "fixture state lock poisoned")?;
    match spec["kind"].as_str().unwrap() {
        "baseline" => baseline(&mut stream, &method, suffix, body, spec, &mut state),
        "workflow" => workflow(
            &mut stream,
            &method,
            suffix,
            body,
            &parsed,
            spec,
            &mut state,
        ),
        "long" => long_workflow(
            &mut stream,
            &method,
            parsed.path(),
            body,
            &parsed,
            spec,
            &mut state,
        ),
        _ => reply(&mut stream, 404, "text/plain", "not found", None),
    }
}

fn reply(
    stream: &mut TcpStream,
    status: u16,
    kind: &str,
    body: &str,
    location: Option<&str>,
) -> Result<()> {
    let phrase = match status {
        200 => "OK",
        303 => "See Other",
        400 => "Bad Request",
        _ => "Not Found",
    };
    let location = location
        .map(|value| format!("Location: {value}\r\n"))
        .unwrap_or_default();
    write!(
        stream,
        "HTTP/1.1 {status} {phrase}\r\nContent-Type: {kind}\r\nContent-Length: {}\r\nCache-Control: no-store\r\nConnection: close\r\n{location}\r\n{body}",
        body.len()
    )?;
    Ok(())
}

fn baseline(
    stream: &mut TcpStream,
    method: &str,
    suffix: &str,
    body: &str,
    spec: &Value,
    state: &mut State,
) -> Result<()> {
    let name = spec["name"].as_str().unwrap();
    if method == "POST" && suffix == "event" {
        let payload: Value = serde_json::from_str(body)?;
        if !payload.is_object() {
            return reply(stream, 400, "application/json", "{}", None);
        }
        state.events.push(
            json!({"name":payload["name"],"query":payload["query"],"order":payload["order"]}),
        );
        let status = match (name, payload["name"].as_str().unwrap_or("")) {
            ("click", "activate") => {
                state.activated += 1;
                "Activated".to_owned()
            }
            ("search", "search") => {
                state.query = payload["query"].clone();
                format!("Results for {}", state.query.as_str().unwrap_or("None"))
            }
            ("select", "sort") => {
                state.order = payload["order"].clone();
                if state.order == "newest" {
                    "Sorted newest first"
                } else {
                    "Sorted oldest first"
                }
                .to_owned()
            }
            ("counter", "increment") => {
                state.count += 1;
                format!("Count: {}", state.count)
            }
            _ => String::new(),
        };
        return reply(
            stream,
            200,
            "application/json",
            &json!({"status":status}).to_string(),
            None,
        );
    }
    if method != "GET" || !(suffix.is_empty() || (suffix == "details" && name == "navigate")) {
        return reply(stream, 404, "text/plain", "not found", None);
    }
    let body = if suffix == "details" {
        state.details_visits += 1;
        "<h1>Observatory details</h1><p id=\"status\">Observatory details opened</p>"
    } else {
        spec["body"].as_str().unwrap()
    };
    let page = format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><title>Benchmark {name}</title><body>{body}{}</body></html>",
        spec["script"].as_str().unwrap()
    );
    reply(stream, 200, "text/html; charset=utf-8", &page, None)
}

fn workflow(
    stream: &mut TcpStream,
    method: &str,
    suffix: &str,
    body: &str,
    url: &reqwest::Url,
    spec: &Value,
    state: &mut State,
) -> Result<()> {
    let name = spec["name"].as_str().unwrap();
    if method == "POST" && suffix == "events" {
        let event: Value = serde_json::from_str(body)?;
        state.events.push(event);
        return reply(stream, 200, "application/json", "{\"ok\":true}", None);
    }
    if method != "GET" {
        return reply(stream, 404, "text/plain", "not found", None);
    }
    if suffix.is_empty() {
        return reply(
            stream,
            200,
            "text/html; charset=utf-8",
            &WORKFLOWS.replace("__CASE_ID__", name),
            None,
        );
    }
    let query = query_values(url);
    let status = if name == "hotel" && suffix.starts_with("property/") {
        let property = suffix.split('/').nth(1).unwrap_or("");
        let mut data = query.clone();
        data["property"] = json!(property);
        state.events.push(json!({"name":"property","data":data}));
        format!(
            "{} | {} | {} | {}",
            if property == "casa-flora" {
                "Casa Flora"
            } else {
                "Casa Marina"
            },
            query["destination"].as_str().unwrap_or(""),
            if query["category"] == "design" {
                "Design"
            } else {
                "Standard"
            },
            if query["cancellation"] == "true" {
                "Free cancellation"
            } else {
                "Non-refundable"
            }
        )
    } else if name == "delayed_menu" && suffix.starts_with("news/") {
        let section = suffix.split('/').nth(1).unwrap_or("");
        state
            .events
            .push(json!({"name":"section","data":{"section":section}}));
        let mut chars = section.chars();
        format!(
            "{}{} headlines",
            chars
                .next()
                .map(|c| c.to_uppercase().to_string())
                .unwrap_or_default(),
            chars.as_str()
        )
    } else if name == "article" && suffix.starts_with("article/") {
        let slug = suffix.split('/').nth(1).unwrap_or("");
        state
            .events
            .push(json!({"name":"article","data":{"slug":slug}}));
        if slug == "godel-incompleteness" {
            "Gödel's incompleteness theorems"
        } else {
            "Kurt Gödel biography"
        }
        .to_owned()
    } else {
        return reply(stream, 404, "text/plain", "not found", None);
    };
    reply(
        stream,
        200,
        "text/html; charset=utf-8",
        &format!(
            "<!doctype html><meta charset=\"utf-8\"><p id=\"status\">{}</p>",
            escape(&status)
        ),
        None,
    )
}

fn query_values(url: &reqwest::Url) -> Value {
    let mut values = serde_json::Map::new();
    for (key, value) in url.query_pairs() {
        if !value.is_empty() {
            values
                .entry(key.into_owned())
                .or_insert_with(|| json!(value));
        }
    }
    Value::Object(values)
}

fn stage_path(name: &str, cursor: usize, total: usize) -> String {
    if cursor == total {
        format!("/{name}/summary")
    } else if cursor == 0 {
        format!("/{name}/")
    } else {
        format!("/{name}/stage/{cursor}")
    }
}

fn long_workflow(
    stream: &mut TcpStream,
    method: &str,
    path: &str,
    body: &str,
    url: &reqwest::Url,
    spec: &Value,
    state: &mut State,
) -> Result<()> {
    let name = spec["name"].as_str().unwrap();
    let sections = spec["sections"].as_array().unwrap();
    let cursor = state.events.len();
    let current = stage_path(name, cursor, sections.len());
    if method == "GET" && path == current {
        return reply(
            stream,
            200,
            "text/html; charset=utf-8",
            &render(spec, &state.events, ""),
            None,
        );
    }
    if cursor == sections.len() || path != stage_path(name, cursor + 1, sections.len()) {
        return reply(stream, 303, "text/plain", "", Some(&current));
    }
    let section = &sections[cursor];
    let alternatives = section["alternatives"].as_array().unwrap();
    let mut fields = json!({});
    let action = if method == "GET" && !alternatives.is_empty() {
        let query = query_values(url);
        let Some(index) = query["choice"]
            .as_str()
            .and_then(|s| s.parse::<usize>().ok())
        else {
            return reply(stream, 404, "text/plain", "not found", None);
        };
        let Some(choice) = alternatives.get(index) else {
            return reply(stream, 404, "text/plain", "not found", None);
        };
        choice.clone()
    } else if method == "POST" && url.query().is_none() && alternatives.is_empty() {
        let form = query_values(&reqwest::Url::parse(&format!("http://localhost/?{body}"))?);
        for (i, field) in section["fields"].as_array().unwrap().iter().enumerate() {
            let raw = form[format!("f{i}")].as_str().unwrap_or("");
            let options = field["options"].as_array().unwrap();
            let value = if field["checkbox"] == true {
                if raw == "on" { "on" } else { "off" }
            } else {
                raw
            };
            if field["checkbox"] != true
                && (value.trim().is_empty()
                    || (!options.is_empty() && !options.iter().any(|option| option == value)))
            {
                return reply(
                    stream,
                    200,
                    "text/html; charset=utf-8",
                    &render(spec, &state.events, "Complete each required field."),
                    None,
                );
            }
            fields[field["label"].as_str().unwrap()] = json!(value);
        }
        section["action"].clone()
    } else {
        return reply(stream, 303, "text/plain", "", Some(&current));
    };
    state.events.push(json!({"action":action,"fields":fields}));
    reply(
        stream,
        200,
        "text/html; charset=utf-8",
        &render(spec, &state.events, ""),
        None,
    )
}

fn escape(raw: &str) -> String {
    raw.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

fn render(spec: &Value, records: &[Value], error: &str) -> String {
    let sections = spec["sections"].as_array().unwrap();
    let name = spec["name"].as_str().unwrap();
    let mut saved = Vec::new();
    for (section, record) in sections.iter().zip(records) {
        let details = section["fields"]
            .as_array()
            .unwrap()
            .iter()
            .map(|field| {
                let label = field["label"].as_str().unwrap();
                format!(
                    "{label}: {}",
                    record["fields"][label].as_str().unwrap_or("")
                )
            })
            .collect::<Vec<_>>()
            .join(", ");
        let suffix = if details.is_empty() {
            String::new()
        } else {
            format!(" — {details}")
        };
        saved.push(format!(
            "<li>{}</li>",
            escape(&format!(
                "{}: {}{suffix}",
                section["heading"].as_str().unwrap(),
                record["action"].as_str().unwrap()
            ))
        ));
    }
    let history = if saved.is_empty() {
        String::new()
    } else {
        format!(
            "<h2>Saved selections</h2><ol id=\"saved-selections\">{}</ol>",
            saved.join("\n")
        )
    };
    let content = if records.len() == sections.len() {
        format!(
            "<p id=\"status\">{} saved</p>{history}",
            escape(spec["title"].as_str().unwrap())
        )
    } else {
        let section = &sections[records.len()];
        let mut html = format!(
            "<h2>{}</h2><p>{}</p>",
            escape(section["heading"].as_str().unwrap()),
            escape(section["prompt"].as_str().unwrap())
        );
        if !error.is_empty() {
            html += &format!("<p role=\"alert\">{}</p>", escape(error));
        }
        let path = stage_path(name, records.len() + 1, sections.len());
        let alternatives = section["alternatives"].as_array().unwrap();
        if !alternatives.is_empty() {
            for (index, choice) in alternatives.iter().enumerate() {
                html += &format!(
                    "<p><a href=\"{path}?choice={index}\">{}</a></p>",
                    escape(choice.as_str().unwrap())
                );
            }
        } else {
            html += &format!("<form action=\"{path}\" method=\"post\">");
            for (index, field) in section["fields"].as_array().unwrap().iter().enumerate() {
                html += &format!(
                    "<p><label for=\"field-{index}\">{}</label> ",
                    escape(field["label"].as_str().unwrap())
                );
                let options = field["options"].as_array().unwrap();
                if field["checkbox"] == true {
                    html += &format!(
                        "<input type=\"checkbox\" id=\"field-{index}\" name=\"f{index}\">"
                    );
                } else if !options.is_empty() {
                    html += &format!(
                        "<select required id=\"field-{index}\" name=\"f{index}\"><option value=\"\">Choose an option</option>"
                    );
                    for option in options {
                        html += &format!("<option>{}</option>", escape(option.as_str().unwrap()));
                    }
                    html += "</select>";
                } else {
                    html += &format!(
                        "<input required id=\"field-{index}\" name=\"f{index}\" autocomplete=\"off\">"
                    );
                }
                html += "</p>";
            }
            html += &format!(
                "<button type=\"submit\">{}</button></form>",
                escape(section["action"].as_str().unwrap())
            );
        }
        html + &history
    };
    format!(
        "<!doctype html><html lang=\"en\"><meta charset=\"utf-8\"><title>{}</title><meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><style>body{{font:18px system-ui;max-width:800px;margin:28px auto;padding:0 18px}}input,button,select{{font:inherit}}p{{margin:.6em 0}}li{{margin:.2em 0}}</style><body><main><h1>{}</h1>{content}</main></body></html>",
        escape(spec["title"].as_str().unwrap()),
        escape(spec["title"].as_str().unwrap())
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn local_client() -> reqwest::blocking::Client {
        reqwest::blocking::Client::builder()
            .no_proxy()
            .timeout(Duration::from_secs(5))
            .build()
            .unwrap()
    }

    #[test]
    fn all_frozen_cases_have_budgets_and_checks() {
        assert_eq!(names().len(), 21);
        for name in names() {
            let fixture = start(&name).unwrap();
            assert!(!fixture.goal.is_empty());
            assert!(!fixture.checks.as_array().unwrap().is_empty());
            assert!(fixture.max_steps > 0 && fixture.max_calls > 0);
            assert!(fixture.timeout_seconds > 0 && fixture.max_input_bytes > 0);
            assert!(fixture.values.is_object());
            if name != "public_about" {
                assert!(!fixture.verify().unwrap());
                let response = local_client().get(&fixture.url).send().unwrap();
                assert_eq!(response.status(), 200);
                assert!(response.text().unwrap().contains("<!doctype html>"));
            }
        }
    }

    #[test]
    fn baseline_counter_requires_exactly_three_recorded_clicks() {
        let fixture = start("counter").unwrap();
        let client = local_client();
        for n in 1..=4 {
            let response = client
                .post(format!("{}event", fixture.url))
                .json(&json!({"name":"increment"}))
                .send()
                .unwrap();
            assert_eq!(response.status(), 200);
            assert_eq!(fixture.verify().unwrap(), n == 3);
        }
    }

    #[test]
    fn long_workflow_records_browser_values_and_blocks_guessed_receipts() {
        let fixture = start("exhibit_planner").unwrap();
        let response = local_client()
            .get(format!("{}summary", fixture.url))
            .send()
            .unwrap();
        assert_eq!(response.url().as_str(), fixture.url);
        assert!(!fixture.verify().unwrap());
        let client = local_client();
        let response = client
            .post(format!("{}stage/1", fixture.url))
            .header("Content-Type", "application/x-www-form-urlencoded")
            .body("f0=Bristol&f1=Spring")
            .send()
            .unwrap();
        assert_eq!(response.status(), 200);
        assert_eq!(
            fixture.oracle().unwrap()["observed"],
            json!([{
                "action":"Explore exhibitions","fields":{"Museum city":"Bristol","Season":"Spring"}
            }])
        );
        assert!(!fixture.verify().unwrap());
    }

    #[test]
    fn hotel_oracle_accepts_filter_order_and_rejects_duplicate_effects() {
        let fixture = start("hotel").unwrap();
        let expected = fixture.spec["expected_events"].as_array().unwrap();
        {
            let mut state = fixture.state.lock().unwrap();
            state.events = vec![
                expected[2].clone(),
                expected[0].clone(),
                expected[1].clone(),
                expected[3].clone(),
            ];
        }
        assert!(fixture.verify().unwrap());
        fixture
            .state
            .lock()
            .unwrap()
            .events
            .push(expected[3].clone());
        assert!(!fixture.verify().unwrap());
    }

    #[test]
    fn every_long_workflow_preserves_exact_section_records() {
        for name in names() {
            let fixture = start(&name).unwrap();
            if fixture.spec["kind"] != "long" {
                continue;
            }
            let client = local_client();
            let sections = fixture.spec["sections"].as_array().unwrap();
            for (cursor, section) in sections.iter().enumerate() {
                let path = stage_path(&name, cursor + 1, sections.len());
                let mut destination = reqwest::Url::parse(&fixture.url).unwrap();
                destination.set_path(&path);
                let alternatives = section["alternatives"].as_array().unwrap();
                let response = if alternatives.is_empty() {
                    let mut encoded = reqwest::Url::parse("http://localhost/").unwrap();
                    for (index, field) in section["fields"].as_array().unwrap().iter().enumerate() {
                        encoded
                            .query_pairs_mut()
                            .append_pair(&format!("f{index}"), field["expected"].as_str().unwrap());
                    }
                    client
                        .post(destination)
                        .header("Content-Type", "application/x-www-form-urlencoded")
                        .body(encoded.query().unwrap_or("").to_owned())
                        .send()
                        .unwrap()
                } else {
                    let index = alternatives
                        .iter()
                        .position(|value| value == &section["action"])
                        .unwrap();
                    destination
                        .query_pairs_mut()
                        .append_pair("choice", &index.to_string());
                    client.get(destination).send().unwrap()
                };
                assert_eq!(response.status(), 200, "{name} section {cursor}");
                let page = response.text().unwrap();
                assert!(page.contains("saved-selections"));
            }
            assert!(fixture.verify().unwrap(), "{name}");
            assert_eq!(
                fixture.oracle().unwrap()["observed"],
                fixture.spec["expected_events"]
            );
        }
    }
}
