use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::thread;
use std::time::Duration;

const RESPONSE_TIMEOUT: Duration = Duration::from_secs(3);

struct Client {
    child: Child,
    input: ChildStdin,
    messages: Receiver<Result<Value, String>>,
}

impl Client {
    fn initialize(capabilities: Value) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_decision-browser-rust-cdp"))
            .env("DECISION_BROWSER_PROVIDER", "fixture")
            .env(
                "DECISION_BROWSER_CHROME",
                std::env::temp_dir().join(format!(
                    "decision-browser-missing-chrome-{}",
                    std::process::id()
                )),
            )
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("start the compiled Rust MCP server");
        let input = child.stdin.take().expect("server stdin");
        let output = child.stdout.take().expect("server stdout");
        let (sender, messages) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let message = match line {
                    Ok(line) => serde_json::from_str(&line)
                        .map_err(|error| format!("invalid JSON-RPC output {line:?}: {error}")),
                    Err(error) => Err(format!("read server stdout: {error}")),
                };
                if sender.send(message).is_err() {
                    break;
                }
            }
        });
        let mut client = Self {
            child,
            input,
            messages,
        };
        client.send(json!({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": capabilities,
                "clientInfo": {"name": "rust-counter-protocol-test", "version": "1"}
            }
        }));
        let response = client.receive();
        assert_eq!(response["id"], 1);
        assert_eq!(response["result"]["protocolVersion"], "2025-11-25");
        client.send(json!({"jsonrpc": "2.0", "method": "notifications/initialized"}));
        client
    }

    fn send(&mut self, message: Value) {
        serde_json::to_writer(&mut self.input, &message).expect("write JSON-RPC message");
        self.input
            .write_all(b"\n")
            .expect("write message delimiter");
        self.input.flush().expect("flush JSON-RPC message");
    }

    fn receive(&self) -> Value {
        self.messages
            .recv_timeout(RESPONSE_TIMEOUT)
            .expect("MCP server response before deadline")
            .expect("valid JSON-RPC output")
    }

    fn call_counter(&mut self, id: u32) {
        self.send(json!({
            "jsonrpc": "2.0",
            "id": id,
            "method": "tools/call",
            "params": {"name": "run_counter", "arguments": {}}
        }));
    }

    fn elicitation(&self) -> Value {
        let request = self.receive();
        assert_eq!(request["method"], "elicitation/create");
        assert!(request.get("id").is_some());
        assert_eq!(request["params"]["mode"], "form");
        assert_eq!(
            request["params"]["requestedSchema"]["properties"]["reviewed"]["type"],
            "boolean"
        );
        assert_eq!(
            request["params"]["requestedSchema"]["required"],
            json!(["reviewed"])
        );
        assert_eq!(
            request["params"]["requestedSchema"]["additionalProperties"],
            false
        );
        request["id"].clone()
    }

    fn respond(&mut self, id: Value, result: Value) {
        self.send(json!({"jsonrpc": "2.0", "id": id, "result": result}));
    }

    fn status(&self, id: u32, expected: &str) {
        let response = self.receive();
        assert_eq!(response["id"], id, "unexpected response {response}");
        assert_eq!(
            response["result"]["structuredContent"]["status"], expected,
            "unexpected response {response}"
        );
    }

    fn remains_pending(&mut self) {
        assert!(matches!(
            self.messages.recv_timeout(Duration::from_millis(100)),
            Err(RecvTimeoutError::Timeout)
        ));
        assert!(
            self.child
                .try_wait()
                .expect("inspect server process")
                .is_none()
        );
    }
}

impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn form_client() -> Client {
    Client::initialize(json!({"elicitation": {"form": {}}}))
}

#[test]
fn lists_the_counter_tool() {
    let mut client = form_client();
    client.send(json!({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}));
    let response = client.receive();
    assert_eq!(response["id"], 2);
    let tools = response["result"]["tools"].as_array().expect("tool list");
    assert!(tools.iter().any(|tool| tool["name"] == "run_counter"));
}

#[test]
fn missing_elicitation_capability_blocks_the_run() {
    let mut client = Client::initialize(json!({}));
    client.call_counter(2);
    client.status(2, "blocked");
}

#[test]
fn url_only_capability_blocks_the_run() {
    let mut client = Client::initialize(json!({"elicitation": {"url": {}}}));
    client.call_counter(2);
    client.status(2, "blocked");
}

#[test]
fn legacy_empty_elicitation_capability_allows_a_form() {
    let mut client = Client::initialize(json!({"elicitation": {}}));
    client.call_counter(2);
    let id = client.elicitation();
    client.respond(id, json!({"action": "decline"}));
    client.status(2, "declined");
}

#[test]
fn decline_and_cancel_never_reach_the_browser() {
    for (action, expected) in [("decline", "declined"), ("cancel", "cancelled")] {
        let mut client = form_client();
        client.call_counter(2);
        let id = client.elicitation();
        client.remains_pending();
        client.respond(id, json!({"action": action}));
        client.status(2, expected);
    }
}

#[test]
fn malformed_confirmations_never_reach_the_browser() {
    for result in [
        json!({"action": "accept", "content": {"reviewed": false}}),
        json!({"action": "accept", "content": {"reviewed": "true"}}),
        json!({"action": "accept", "content": {}}),
        json!({"action": "accept", "content": {"reviewed": true, "extra": true}}),
        json!({"action": "accept"}),
        json!({"action": "approve", "content": {"reviewed": true}}),
        json!({}),
        Value::Null,
    ] {
        let mut client = form_client();
        client.call_counter(2);
        let id = client.elicitation();
        client.respond(id, result);
        client.status(2, "blocked");
    }
}

#[test]
fn an_unrelated_response_cannot_approve_a_pending_run() {
    let mut client = form_client();
    client.call_counter(2);
    let id = client.elicitation();
    client.respond(
        json!("unrelated-elicitation-id"),
        json!({"action": "accept", "content": {"reviewed": true}}),
    );
    client.remains_pending();
    client.respond(id, json!({"action": "decline"}));
    client.status(2, "declined");
}

#[test]
fn acceptance_is_required_again_for_each_counter_call() {
    let mut client = form_client();
    client.call_counter(2);
    let first_id = client.elicitation();
    client.remains_pending();
    client.respond(
        first_id.clone(),
        json!({"action": "accept", "content": {"reviewed": true}}),
    );
    // The deliberately missing browser is reached only after valid acceptance.
    client.status(2, "failed");
    client.call_counter(3);
    let second_id = client.elicitation();
    assert_ne!(first_id, second_id);
    client.respond(
        first_id,
        json!({"action": "accept", "content": {"reviewed": true}}),
    );
    client.remains_pending();
    client.respond(second_id, json!({"action": "decline"}));
    client.status(3, "declined");
}
