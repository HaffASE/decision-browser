use serde_json::{Value, json};
use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

struct Client {
    child: Child,
    input: ChildStdin,
    messages: Receiver<Value>,
}
impl Client {
    fn start() -> Self {
        Self::with_capabilities(json!({"elicitation":{"form":{}}}))
    }
    fn with_capabilities(capabilities: Value) -> Self {
        let mut command = Command::new(env!("CARGO_BIN_EXE_decision-browser"));
        command
            .args(["serve", "--admission", "standard_form"])
            .env("DECISION_BROWSER_PROVIDER", "openai")
            .env(
                "DECISION_BROWSER_CONFIG_FILE",
                std::env::temp_dir().join("decision-browser-absent-test-profile.json"),
            )
            .env_remove("OPENAI_API_KEY")
            .env_remove("TYPESAFE_API_KEY")
            .env_remove("OPENCODE_API_KEY");
        Self::spawn(command, capabilities)
    }
    fn spawn(mut command: Command, capabilities: Value) -> Self {
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let input = child.stdin.take().unwrap();
        let output = child.stdout.take().unwrap();
        let (tx, messages) = mpsc::channel();
        std::thread::spawn(move || {
            for line in BufReader::new(output).lines() {
                let Ok(line) = line else {
                    break;
                };
                let message = serde_json::from_str(&line).unwrap();
                if tx.send(message).is_err() {
                    break;
                }
            }
        });
        let mut client = Self {
            child,
            input,
            messages,
        };
        client.send(json!({"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":capabilities,"clientInfo":{"name":"product-protocol-test","version":"1"}}}));
        assert_eq!(client.receive()["result"]["protocolVersion"], "2025-11-25");
        client.send(json!({"jsonrpc":"2.0","method":"notifications/initialized"}));
        client
    }
    fn send(&mut self, message: Value) {
        serde_json::to_writer(&mut self.input, &message).unwrap();
        self.input.write_all(b"\n").unwrap();
        self.input.flush().unwrap();
    }
    fn receive(&self) -> Value {
        self.messages.recv_timeout(Duration::from_secs(4)).unwrap()
    }
    fn tool(&mut self, name: &str, args: Value) -> Value {
        self.send(json!({"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":name,"arguments":args}}));
        self.receive()
    }
}
impl Drop for Client {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}
#[test]
fn native_discovery_has_all_six_documented_tools() {
    let mut client = Client::start();
    client.send(json!({"jsonrpc":"2.0","id":1,"method":"tools/list"}));
    let answer = client.receive();
    let tools = answer["result"]["tools"].as_array().unwrap();
    let mut names = tools
        .iter()
        .map(|tool| tool["name"].as_str().unwrap())
        .collect::<Vec<_>>();
    names.sort();
    assert_eq!(
        names,
        [
            "cancel_task",
            "capabilities",
            "get_task",
            "present_admission",
            "read_artifact",
            "start_task"
        ]
    );
    for tool in tools {
        assert!(tool["description"].as_str().unwrap().len() > 20);
        assert!(tool["annotations"].is_object());
        assert!(tool["outputSchema"].is_object());
    }
}
#[test]
fn reads_report_prerequisites_and_unknown_runs_without_starting_work() {
    let mut client = Client::start();
    let caps = client.tool("capabilities", json!({}));
    assert_eq!(
        caps["result"]["structuredContent"]["modes"],
        json!(["isolated", "existing_chrome"])
    );
    assert_eq!(
        caps["result"]["structuredContent"]["production_ready"],
        false
    );
    assert_eq!(
        caps["result"]["structuredContent"]["admission_mode"],
        "standard_form"
    );
    assert_eq!(
        caps["result"]["structuredContent"]["native_human"],
        "NOT_RUN"
    );
    let recovery = client.tool("get_task", json!({}));
    assert_eq!(
        recovery["result"]["structuredContent"],
        json!({"error":"unknown"})
    );
    assert_eq!(recovery["result"]["isError"], true);
}
#[test]
fn task_scope_existing_chrome_requires_a_valid_explicit_selection() {
    let selected = std::env::temp_dir().join(format!(
        "decision-browser-devtools-test-{}-{}",
        std::process::id(),
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    let mut options = std::fs::OpenOptions::new();
    options.write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    writeln!(
        options.open(&selected).unwrap(),
        "9\n/devtools/browser/protocol-test"
    )
    .unwrap();

    // Checkout is blocked by the worker before any provider call or attachment.
    let request = json!({"request":{
        "goal":"Open the checkout page", "start_url":"https://example.org/checkout",
        "mode":"existing_chrome", "values":{}, "checks":[], "privacy":{},
        "limits":{"max_seconds":5.0}
    }});
    for (path, available) in [
        (None, false),
        (Some(Path::new("DevToolsActivePort")), false),
        (Some(selected.as_path()), true),
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_decision-browser"));
        command
            .args(["serve", "--admission", "task_scope"])
            .env_clear()
            .env("DECISION_BROWSER_PROVIDER", "local");
        if let Some(path) = path {
            command.env("DECISION_BROWSER_DEVTOOLS_ACTIVE_PORT", path);
        }
        let mut client = Client::spawn(command, json!({"elicitation":{"form":{}}}));
        let caps = client.tool("capabilities", json!({}));
        let caps = &caps["result"]["structuredContent"];
        let start = client.tool("start_task", request.clone());
        let result = &start["result"]["structuredContent"];
        assert_eq!(caps["admission_mode"], "task_scope");
        assert!(caps["human_grant_channel"].is_null());
        if available {
            assert_eq!(caps["modes"], json!(["isolated", "existing_chrome"]));
            assert_eq!(result["result"]["terminal"], "queued", "{start}");
            assert!(result["pending_proposal"].is_null());
        } else {
            assert_eq!(caps["modes"], json!(["isolated"]));
            assert_eq!(
                result["error"], "task_scope_existing_chrome_unavailable",
                "{start}"
            );
            assert_eq!(start["result"]["isError"], true);
        }
    }
    std::fs::remove_file(selected).unwrap();
}
#[test]
fn unknown_or_coerced_arguments_are_rejected_before_start() {
    let mut client = Client::start();
    for (name, args) in [
        ("capabilities", json!({"extra":true})),
        ("get_task", json!({"run_id":1})),
        (
            "read_artifact",
            json!({"run_id":"r","artifact_id":"../../private","offset":"0"}),
        ),
        ("start_task", json!({"request":"{}"})),
        (
            "present_admission",
            json!({"run_id":"r","proposal_id":"p","digest":"bad"}),
        ),
    ] {
        let result = client.tool(name, args);
        assert_eq!(result["error"]["code"], -32602, "{result}");
    }
}

#[test]
fn retained_task_requires_the_bound_form_and_decline_prevents_execution() {
    let mut client = Client::start();
    let started = client.tool(
        "start_task",
        json!({"request":{
            "goal":"Observe the local test page", "start_url":"http://127.0.0.1:9/",
            "values":{},"checks":[],"limits":{"max_seconds":5.0},"privacy":{}
        }}),
    );
    let run = started["result"]["structuredContent"]["result"]["run_id"].clone();
    assert!(run.is_string(), "{started}");
    let mut current = started;
    for _ in 0..100 {
        if current["result"]["structuredContent"]["pending_proposal"].is_object() {
            break;
        }
        std::thread::sleep(Duration::from_millis(10));
        current = client.tool("get_task", json!({"run_id":run}));
    }
    let scope = &current["result"]["structuredContent"]["pending_proposal"];
    assert!(scope.is_object(), "{current}");
    let busy = client.tool(
        "start_task",
        json!({"request":{
            "goal":"Observe the local test page", "start_url":"http://127.0.0.1:9/",
            "values":{},"checks":[],"limits":{},"privacy":{}
        }}),
    );
    assert_eq!(busy["result"]["structuredContent"]["error"], "busy");
    client.send(json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"present_admission","arguments":{"run_id":run,"proposal_id":scope["proposal_id"],"digest":scope["digest"]}}}));
    let form = client.receive();
    assert_eq!(form["method"], "elicitation/create");
    client.send(json!({"jsonrpc":"2.0","id":form["id"],"result":{"action":"decline"}}));
    let response = client.receive();
    assert_eq!(response["id"], 3);
    assert_eq!(
        response["result"]["structuredContent"]["result"]["terminal"],
        "blocked"
    );
    let recovered = client.tool("get_task", json!({}));
    assert_eq!(
        recovered["result"]["structuredContent"]["result"]["run_id"],
        run
    );
}

fn pending_task(client: &mut Client, seconds: f64) -> Value {
    let mut snapshot = client.tool(
        "start_task",
        json!({"request":{
            "goal":"Observe the local test page", "start_url":"http://127.0.0.1:9/",
            "values":{},"checks":[],"limits":{"max_seconds":seconds},"privacy":{}
        }}),
    );
    for _ in 0..100 {
        if snapshot["result"]["structuredContent"]["pending_proposal"].is_object() {
            return snapshot["result"]["structuredContent"]["pending_proposal"].clone();
        }
        std::thread::sleep(Duration::from_millis(5));
        snapshot = client.tool("get_task", json!({}));
    }
    panic!("pending approval not observed: {snapshot}")
}
fn present(client: &mut Client, scope: &Value) {
    client.send(json!({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"present_admission","arguments":{"run_id":scope["run_id"],"proposal_id":scope["proposal_id"],"digest":scope["digest"]}}}));
}
fn stable_terminal(client: &mut Client, terminal: &str, reason: &str) {
    // Observe repeatedly after the worker settles, catching a later overwrite
    // of the admission snapshot by the execution error mapping.
    for _ in 0..15 {
        std::thread::sleep(Duration::from_millis(10));
        let snapshot = client.tool("get_task", json!({}));
        let result = &snapshot["result"]["structuredContent"]["result"];
        assert_eq!(result["terminal"], terminal, "{snapshot}");
        assert_eq!(result["reason"], reason, "{snapshot}");
        assert_eq!(result["usage"]["calls"], 0);
        assert_eq!(result["usage"]["steps"], 0);
    }
}
#[test]
fn form_decline_and_cancel_survive_worker_finalization() {
    for (action, terminal, reason) in [
        ("decline", "blocked", "admission_declined"),
        ("cancel", "cancelled", "cancelled"),
    ] {
        let mut client = Client::start();
        let scope = pending_task(&mut client, 5.0);
        present(&mut client, &scope);
        let form = client.receive();
        assert_eq!(form["method"], "elicitation/create");
        client.send(json!({"jsonrpc":"2.0","id":form["id"],"result":{"action":action}}));
        assert_eq!(client.receive()["id"], 3);
        stable_terminal(&mut client, terminal, reason);
    }
}
#[test]
fn form_expiry_survives_worker_finalization() {
    let mut client = Client::start();
    let scope = pending_task(&mut client, 0.4);
    present(&mut client, &scope);
    assert_eq!(client.receive()["method"], "elicitation/create");
    assert_eq!(client.receive()["id"], 3);
    stable_terminal(&mut client, "blocked", "admission_expired");
}
#[test]
fn unsupported_form_client_preserves_the_admission_reason() {
    let mut client = Client::with_capabilities(json!({"elicitation":{"url":{}}}));
    let scope = pending_task(&mut client, 5.0);
    present(&mut client, &scope);
    let response = client.receive();
    assert_eq!(
        response["result"]["structuredContent"]["error"],
        "no_supported_human_grant_channel"
    );
    stable_terminal(&mut client, "blocked", "no_supported_human_grant_channel");
}
