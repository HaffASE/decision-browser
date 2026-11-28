use base64::{Engine, engine::general_purpose::STANDARD};
use decision_browser_rust_cdp::{browser, provider::Provider};
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant};

const TOOL: &str = "run_counter";
const PROTOCOL: &str = "2025-11-25";
const APPROVAL_TIMEOUT: Duration = Duration::from_secs(120);

struct Pending {
    tool_id: Value,
    form_id: Value,
    deadline: Instant,
}

enum Event {
    Input(io::Result<String>),
    Closed,
    Finished { form_id: Value, response: Value },
}

struct Active {
    form_id: Value,
    tool_id: Value,
    cancelled: Arc<AtomicBool>,
    worker: JoinHandle<()>,
}

struct Server {
    provider: Provider,
    received_initialize: bool,
    initialized: bool,
    form_supported: bool,
    next_form: u64,
    pending: Option<Pending>,
    active: Option<Active>,
    events: mpsc::Sender<Event>,
}

impl Server {
    fn new(provider: Provider, events: mpsc::Sender<Event>) -> Self {
        Self {
            provider,
            received_initialize: false,
            initialized: false,
            form_supported: false,
            next_form: 0,
            pending: None,
            active: None,
            events,
        }
    }

    fn message(&mut self, message: &Value) -> Option<Value> {
        if message.get("jsonrpc").and_then(Value::as_str) != Some("2.0") {
            return Some(error(Value::Null, -32600, "Invalid JSON-RPC request."));
        }
        let id = message.get("id");
        let params = &message["params"];
        let Some(method) = message.get("method").and_then(Value::as_str) else {
            return self.form_response(message);
        };
        let Some(id) = id else {
            return self.notification(method, params);
        };
        if !(id.is_string() || id.is_i64() || id.is_u64()) {
            return Some(error(Value::Null, -32600, "Invalid JSON-RPC request ID."));
        }

        Some(match method {
            "initialize" if !self.received_initialize && params["protocolVersion"].is_string() => {
                self.received_initialize = true;
                self.form_supported = params["capabilities"]["elicitation"]
                    .as_object()
                    .is_some_and(|caps| {
                        caps.is_empty() || caps.get("form").is_some_and(Value::is_object)
                    });
                reply(
                    id,
                    json!({
                        "protocolVersion": PROTOCOL,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": env!("CARGO_PKG_NAME"), "version": env!("CARGO_PKG_VERSION")}
                    }),
                )
            }
            "initialize" => error(
                id.clone(),
                -32602,
                "Invalid or repeated initialize request.",
            ),
            "ping" => reply(id, json!({})),
            _ if !self.initialized => error(id.clone(), -32002, "MCP session is not initialized."),
            "tools/list" => reply(id, json!({"tools": [tool_definition(self.provider)]})),
            "tools/call" => self.call(id, params),
            _ => error(id.clone(), -32601, "Method not found."),
        })
    }

    fn notification(&mut self, method: &str, params: &Value) -> Option<Value> {
        match method {
            "notifications/initialized" if self.received_initialize => self.initialized = true,
            "notifications/cancelled" => {
                if self
                    .pending
                    .as_ref()
                    .is_some_and(|pending| params.get("requestId") == Some(&pending.tool_id))
                {
                    let pending = self.pending.take().expect("matching pending approval");
                    return Some(status(
                        &pending.tool_id,
                        "cancelled",
                        "The pending counter request was cancelled.",
                    ));
                }
                if let Some(active) = &self.active
                    && params.get("requestId") == Some(&active.tool_id)
                {
                    active.cancelled.store(true, Ordering::Release);
                }
            }
            _ => {}
        }
        None
    }

    fn expire_approval(&mut self, now: Instant) -> Option<Value> {
        let pending = self.pending.take_if(|pending| now >= pending.deadline)?;
        Some(status(
            &pending.tool_id,
            "blocked",
            "The approval expired before the browser was started.",
        ))
    }

    fn disconnect(&mut self) {
        self.pending = None;
        if let Some(active) = &self.active {
            active.cancelled.store(true, Ordering::Release);
        }
    }

    fn finish(&mut self, form_id: &Value, response: Value) -> Option<Value> {
        let active = self.active.take_if(|active| &active.form_id == form_id)?;
        let _ = active.worker.join();
        Some(response)
    }

    fn call(&mut self, id: &Value, params: &Value) -> Value {
        if params["name"] != TOOL
            || params.get("arguments").is_some_and(|arguments| {
                arguments
                    .as_object()
                    .is_none_or(|arguments| !arguments.is_empty())
            })
        {
            return error(
                id.clone(),
                -32602,
                "run_counter accepts an empty argument object.",
            );
        }
        if !self.form_supported {
            return status(
                id,
                "blocked",
                "The MCP client must support form elicitation.",
            );
        }
        if self.pending.is_some() || self.active.is_some() {
            return status(
                id,
                "blocked",
                "A counter request is already pending or running.",
            );
        }

        self.next_form += 1;
        let form_id = json!(format!("counter-approval-{}", self.next_form));
        self.pending = Some(Pending {
            tool_id: id.clone(),
            form_id: form_id.clone(),
            deadline: Instant::now() + APPROVAL_TIMEOUT,
        });
        let mut message = "Allow one click on the increment button of a local test counter in a new browser profile? The expected value is 1.".to_owned();
        if !matches!(self.provider, Provider::Fixture) {
            message.push_str(&format!(
                " Send the synthetic counter state, task instructions, and offered actions to {} for one decision.",
                self.provider.name()
            ));
        }
        json!({
            "jsonrpc": "2.0",
            "id": form_id,
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": message,
                "requestedSchema": {
                    "type": "object",
                    "properties": {"reviewed": {
                        "type": "boolean",
                        "title": "I reviewed the single local counter click"
                    }},
                    "required": ["reviewed"],
                    "additionalProperties": false
                }
            }
        })
    }

    fn form_response(&mut self, message: &Value) -> Option<Value> {
        let pending = self
            .pending
            .take_if(|pending| message.get("id") == Some(&pending.form_id))?;
        let id = &pending.tool_id;
        if Instant::now() >= pending.deadline {
            return Some(status(
                id,
                "blocked",
                "The approval expired before the browser was started.",
            ));
        }
        if message.get("error").is_some() {
            return Some(status(
                id,
                "blocked",
                "The client could not present the confirmation form.",
            ));
        }
        let result = &message["result"];
        match result["action"].as_str() {
            Some("decline") => Some(status(id, "declined", "The local counter was not opened.")),
            Some("cancel") => Some(status(id, "cancelled", "The local counter was not opened.")),
            Some("accept") if result["content"] == json!({"reviewed": true}) => {
                let provider = self.provider;
                let cancelled = Arc::new(AtomicBool::new(false));
                let worker_cancelled = cancelled.clone();
                let events = self.events.clone();
                let form_id = pending.form_id;
                let worker_form_id = form_id.clone();
                let tool_id = pending.tool_id;
                let worker_id = tool_id.clone();
                let worker = thread::spawn(move || {
                    let response = std::panic::catch_unwind(|| {
                        run_counter(&worker_id, &provider, &worker_cancelled)
                    })
                    .unwrap_or_else(|_| {
                        status(&worker_id, "failed", "The counter worker panicked.")
                    });
                    let _ = events.send(Event::Finished {
                        form_id: worker_form_id,
                        response,
                    });
                });
                self.active = Some(Active {
                    form_id,
                    tool_id,
                    cancelled,
                    worker,
                });
                None
            }
            _ => Some(status(
                id,
                "blocked",
                "Confirmation must include reviewed: true.",
            )),
        }
    }
}

fn tool_definition(provider: Provider) -> Value {
    let live = !matches!(provider, Provider::Fixture);
    json!({
        "name": TOOL,
        "title": "Run the local counter test",
        "description": "Request MCP form confirmation, click the local counter once through CDP, and verify that its value changes from 0 to 1. Returns the outcome and a PNG on success.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": false},
        "outputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["blocked", "declined", "cancelled", "failed", "verified"]},
                "before": {"type": "integer"},
                "after": {"type": "integer"},
                "provider": {"type": "string"},
                "model": {"type": "string"},
                "inputTokens": {"type": "integer", "minimum": 0},
                "outputTokens": {"type": "integer", "minimum": 0}
            },
            "required": ["status"],
            "additionalProperties": false
        },
        "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": live}
    })
}

fn run_counter(id: &Value, provider: &Provider, cancelled: &AtomicBool) -> Value {
    if cancelled.load(Ordering::Acquire) {
        return status(
            id,
            "cancelled",
            "The counter request was cancelled before browser startup.",
        );
    }
    let check_cancelled = || -> browser::BrowserResult<()> {
        if cancelled.load(Ordering::Acquire) {
            return Err(io::Error::other("counter_cancelled").into());
        }
        Ok(())
    };
    let mut decision = None;
    let result = browser::run_counter_guarded(
        |label, before| {
            check_cancelled()?;
            decision = Some(provider.choose_increment(label, before)?);
            check_cancelled()
        },
        check_cancelled,
    );
    if cancelled.load(Ordering::Acquire) {
        if let Err(cause) = &result {
            let detail = cause.to_string();
            if detail != "counter_cancelled" {
                return status(
                    id,
                    "failed",
                    &format!(
                        "Cancellation was processed. Browser outcome or cleanup could not be confirmed: {detail}"
                    ),
                );
            }
        }
        return status(
            id,
            "cancelled",
            "Cancellation was processed. An input command already dispatched may have taken effect.",
        );
    }
    match (result, decision) {
        (Ok(evidence), Some(decision)) => {
            let mut facts = json!({
                "status": "verified", "before": evidence.before, "after": evidence.after,
                "provider": provider.name(), "model": decision.model
            });
            if let Some(input_tokens) = decision.input_tokens {
                facts["inputTokens"] = json!(input_tokens);
            }
            if let Some(output_tokens) = decision.output_tokens {
                facts["outputTokens"] = json!(output_tokens);
            }
            reply(
                id,
                json!({
                    "content": [
                        {"type": "text", "text": format!("Verified local counter: {} → {}.", evidence.before, evidence.after)},
                        {"type": "image", "mimeType": "image/png", "data": STANDARD.encode(evidence.png)}
                    ],
                    "structuredContent": facts,
                    "isError": false
                }),
            )
        }
        (Err(cause), _) => status(
            id,
            "failed",
            &format!("Local counter verification failed: {cause}"),
        ),
        (Ok(_), None) => status(id, "failed", "Provider choice was not observed."),
    }
}

fn status(id: &Value, status: &str, text: &str) -> Value {
    reply(
        id,
        json!({
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"status": status},
            "isError": status == "failed" || status == "blocked"
        }),
    )
}

fn reply(id: &Value, result: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "result": result})
}

fn error(id: Value, code: i32, message: &str) -> Value {
    json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
}

fn write_response(stdout: &mut impl Write, response: &Value) -> io::Result<()> {
    serde_json::to_writer(&mut *stdout, response)?;
    stdout.write_all(b"\n")?;
    stdout.flush()
}

fn serve(
    mut server: Server,
    events: mpsc::Receiver<Event>,
    stdout: &mut impl Write,
) -> io::Result<()> {
    let mut connected = true;
    let mut input_error = None;
    loop {
        if connected
            && let Some(response) = server.expire_approval(Instant::now())
            && let Err(error) = write_response(stdout, &response)
        {
            input_error = Some(error);
            connected = false;
            server.disconnect();
        }
        if !connected && server.active.is_none() {
            return input_error.map_or(Ok(()), Err);
        }
        let response = match events.recv_timeout(Duration::from_millis(25)) {
            Ok(Event::Input(Ok(line))) if connected => match serde_json::from_str(&line) {
                Ok(message) => server.message(&message),
                Err(_) => Some(error(Value::Null, -32700, "Invalid JSON.")),
            },
            Ok(Event::Input(Err(error))) => {
                input_error = Some(error);
                connected = false;
                server.disconnect();
                None
            }
            Ok(Event::Closed) => {
                connected = false;
                server.disconnect();
                None
            }
            Ok(Event::Finished { form_id, response }) => server.finish(&form_id, response),
            Ok(_) | Err(mpsc::RecvTimeoutError::Timeout) => None,
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                return Err(io::Error::other("MCP event channel closed unexpectedly"));
            }
        };
        if connected
            && let Some(response) = response
            && let Err(error) = write_response(stdout, &response)
        {
            input_error = Some(error);
            connected = false;
            server.disconnect();
        }
    }
}

fn main() -> io::Result<()> {
    let provider = Provider::from_environment().map_err(io::Error::other)?;
    let (sender, events) = mpsc::channel();
    let input_events = sender.clone();
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            if input_events.send(Event::Input(line)).is_err() {
                return;
            }
        }
        let _ = input_events.send(Event::Closed);
    });
    let server = Server::new(provider, sender);
    serve(server, events, &mut io::stdout().lock())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pending(id: u64, deadline: Instant) -> Pending {
        Pending {
            tool_id: json!(id),
            form_id: json!(format!("approval-{id}")),
            deadline,
        }
    }

    #[test]
    fn approval_expiry_settles_the_original_call_without_stdin_activity() {
        let (sender, events) = mpsc::channel();
        let mut server = Server::new(Provider::Fixture, sender.clone());
        server.pending = Some(pending(2, Instant::now() + Duration::from_millis(10)));
        let closer = thread::spawn(move || {
            thread::sleep(Duration::from_millis(80));
            sender.send(Event::Closed).unwrap();
        });
        let mut output = Vec::new();
        serve(server, events, &mut output).unwrap();
        closer.join().unwrap();
        let lines = String::from_utf8(output).unwrap();
        let response: Value = serde_json::from_str(lines.trim()).unwrap();
        assert_eq!(response["id"], 2);
        assert_eq!(response["result"]["structuredContent"]["status"], "blocked");
    }

    #[test]
    fn cancellation_is_read_while_an_authorized_worker_is_running() {
        let (sender, events) = mpsc::channel();
        let mut server = Server::new(Provider::Fixture, sender.clone());
        server.received_initialize = true;
        server.initialized = true;
        let cancelled = Arc::new(AtomicBool::new(false));
        let observed = cancelled.clone();
        let completion = sender.clone();
        let worker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(1);
            while !observed.load(Ordering::Acquire) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(1));
            }
            assert!(observed.load(Ordering::Acquire));
            completion
                .send(Event::Finished {
                    form_id: json!("approval-2"),
                    response: status(
                        &json!(2),
                        "cancelled",
                        "Synthetic worker stopped before dispatch.",
                    ),
                })
                .unwrap();
            completion.send(Event::Closed).unwrap();
        });
        server.active = Some(Active {
            form_id: json!("approval-2"),
            tool_id: json!(2),
            cancelled,
            worker,
        });
        sender
            .send(Event::Input(Ok(json!({
                "jsonrpc": "2.0", "id": 3, "method": "ping"
            })
            .to_string())))
            .unwrap();
        sender
            .send(Event::Input(Ok(json!({
                "jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}
            })
            .to_string())))
            .unwrap();
        let mut output = Vec::new();
        serve(server, events, &mut output).unwrap();
        let responses: Vec<Value> = String::from_utf8(output)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect();
        assert_eq!(responses.len(), 2);
        assert_eq!(responses[0]["id"], 3);
        assert_eq!(responses[1]["id"], 2);
        assert_eq!(
            responses[1]["result"]["structuredContent"]["status"],
            "cancelled"
        );
    }

    #[test]
    fn eof_cancels_and_waits_for_the_authorized_worker() {
        let (sender, events) = mpsc::channel();
        let mut server = Server::new(Provider::Fixture, sender.clone());
        let cancelled = Arc::new(AtomicBool::new(false));
        let observed = cancelled.clone();
        let completed = Arc::new(AtomicBool::new(false));
        let worker_completed = completed.clone();
        let worker_events = sender.clone();
        let worker = thread::spawn(move || {
            let deadline = Instant::now() + Duration::from_secs(1);
            while !observed.load(Ordering::Acquire) && Instant::now() < deadline {
                thread::sleep(Duration::from_millis(1));
            }
            assert!(observed.load(Ordering::Acquire));
            worker_completed.store(true, Ordering::Release);
            worker_events
                .send(Event::Finished {
                    form_id: json!("approval-2"),
                    response: status(&json!(2), "cancelled", "closed"),
                })
                .unwrap();
        });
        server.active = Some(Active {
            form_id: json!("approval-2"),
            tool_id: json!(2),
            cancelled,
            worker,
        });
        sender.send(Event::Closed).unwrap();
        let mut output = Vec::new();
        serve(server, events, &mut output).unwrap();
        assert!(completed.load(Ordering::Acquire));
        assert!(output.is_empty());
    }
}
