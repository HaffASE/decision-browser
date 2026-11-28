mod browser;
mod provider;

use base64::{Engine, engine::general_purpose::STANDARD};
use provider::Provider;
use serde_json::{Value, json};
use std::io::{self, BufRead, Write};
use std::time::{Duration, Instant};

const TOOL: &str = "run_counter";
const PROTOCOL: &str = "2025-11-25";
const APPROVAL_TIMEOUT: Duration = Duration::from_secs(120);

struct Pending {
    tool_id: Value,
    form_id: Value,
    deadline: Instant,
}

struct Server {
    provider: Provider,
    received_initialize: bool,
    initialized: bool,
    form_supported: bool,
    next_form: u64,
    pending: Option<Pending>,
}

impl Server {
    fn new(provider: Provider) -> Self {
        Self {
            provider,
            received_initialize: false,
            initialized: false,
            form_supported: false,
            next_form: 0,
            pending: None,
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
            self.notification(method, params);
            return None;
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
            "tools/list" => reply(id, json!({"tools": [tool_definition()]})),
            "tools/call" => self.call(id, params),
            _ => error(id.clone(), -32601, "Method not found."),
        })
    }

    fn notification(&mut self, method: &str, params: &Value) {
        match method {
            "notifications/initialized" if self.received_initialize => self.initialized = true,
            "notifications/cancelled"
                if self
                    .pending
                    .as_ref()
                    .is_some_and(|pending| params.get("requestId") == Some(&pending.tool_id)) =>
            {
                self.pending = None
            }
            _ => {}
        }
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
        if self
            .pending
            .as_ref()
            .is_some_and(|pending| Instant::now() >= pending.deadline)
        {
            self.pending = None;
        }
        if self.pending.is_some() {
            return status(id, "blocked", "A counter approval is already pending.");
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
                " Send only the synthetic button label and counter value to {} to choose the action.",
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
        Some(match result["action"].as_str() {
            Some("decline") => status(id, "declined", "The local counter was not opened."),
            Some("cancel") => status(id, "cancelled", "The local counter was not opened."),
            Some("accept") if result["content"] == json!({"reviewed": true}) => {
                run_counter(id, &self.provider)
            }
            _ => status(id, "blocked", "Confirmation must include reviewed: true."),
        })
    }
}

fn tool_definition() -> Value {
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
        "annotations": {"readOnlyHint": false, "destructiveHint": false, "idempotentHint": false, "openWorldHint": false}
    })
}

fn run_counter(id: &Value, provider: &Provider) -> Value {
    let mut decision = None;
    let result = browser::run_counter(|label, before| {
        decision = Some(provider.choose_increment(label, before)?);
        Ok(())
    });
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

fn main() -> io::Result<()> {
    let mut server = Server::new(Provider::from_environment().map_err(io::Error::other)?);
    let mut stdout = io::stdout().lock();
    for line in io::stdin().lock().lines() {
        let response = match serde_json::from_str(&line?) {
            Ok(message) => server.message(&message),
            Err(_) => Some(error(Value::Null, -32700, "Invalid JSON.")),
        };
        if let Some(response) = response {
            serde_json::to_writer(&mut stdout, &response)?;
            stdout.write_all(b"\n")?;
            stdout.flush()?;
        }
    }
    Ok(())
}
