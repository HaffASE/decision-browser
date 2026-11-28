//! Retained six-tool MCP lifecycle with request-bound form admission.
use crate::{
    provider::Provider,
    sha256_hex as sha,
    task_runner::{self, RunControl, TaskOutcome},
    tool_contracts,
};
use base64::{Engine, engine::general_purpose::STANDARD};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Condvar, Mutex, mpsc};
use std::thread::{self, JoinHandle};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const FORM_TTL: Duration = Duration::from_secs(120);
const RETENTION: Duration = Duration::from_secs(120);
const RANGE: usize = 65536;
static NEXT_ID: AtomicU64 = AtomicU64::new(0);

fn new_id(prefix: &str) -> String {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    format!(
        "{prefix}-{now:x}-{:x}",
        NEXT_ID.fetch_add(1, Ordering::Relaxed)
    )
}
fn body(value: Value, error: bool) -> Value {
    json!({"content":[{"type":"text","text":value.to_string()}],"structuredContent":value,"isError":error})
}
fn domain(code: &str) -> Value {
    body(json!({"error":code}), true)
}
fn reply(id: &Value, result: Value) -> Value {
    json!({"jsonrpc":"2.0","id":id,"result":result})
}
fn protocol_error(id: &Value, code: i32, message: &str) -> Value {
    json!({"jsonrpc":"2.0","id":id,"error":{"code":code,"message":message}})
}
fn seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

enum Event {
    Input(io::Result<String>),
    Outbound(Value),
    Closed,
    Proposal(Value),
    Finished(TaskOutcome),
}
struct Gate {
    cancelled: AtomicBool,
    answer: Mutex<Option<Result<(), &'static str>>>,
    ready: Condvar,
    events: mpsc::Sender<Event>,
    deadline: Instant,
}
impl Gate {
    fn answer(&self, answer: Result<(), &'static str>) {
        *self.answer.lock().expect("admission mutex") = Some(answer);
        self.ready.notify_all();
    }
    fn cancel(&self) {
        self.cancelled.store(true, Ordering::Release);
        self.answer(Err("cancelled"));
    }
}
impl RunControl for Gate {
    fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }
    fn approve(&self, proposal: Value) -> Result<(), &'static str> {
        if self.cancelled() {
            return Err("cancelled");
        }
        *self.answer.lock().map_err(|_| "failed")? = None;
        self.events
            .send(Event::Proposal(proposal))
            .map_err(|_| "cancelled")?;
        let answer = self.answer.lock().map_err(|_| "failed")?;
        let (mut answer, _) = self
            .ready
            .wait_timeout_while(
                answer,
                FORM_TTL.min(self.deadline.saturating_duration_since(Instant::now())),
                |answer| answer.is_none() && !self.cancelled(),
            )
            .map_err(|_| "failed")?;
        if self.cancelled() {
            return Err("cancelled");
        }
        answer.take().unwrap_or(Err("admission_expired"))
    }
}
struct Pending {
    scope: Value,
    expires: Instant,
    presentation: Option<(Value, Value)>,
}
struct Artifact {
    bytes: Vec<u8>,
    media: String,
}
struct Run {
    id: String,
    result: Value,
    pending: Option<Pending>,
    gate: Arc<Gate>,
    worker: Option<JoinHandle<()>>,
    completed: Option<Instant>,
    artifacts: BTreeMap<String, Artifact>,
    blocked: Vec<String>,
    max_stored: usize,
    admission_denial: Option<&'static str>,
    admitted_origin: Option<String>,
}
impl Run {
    fn snapshot(&self) -> Value {
        body(
            json!({"result":self.result,"pending_proposal":self.pending.as_ref().map(|pending|&pending.scope)}),
            false,
        )
    }
    fn matches(&self, args: &Value) -> bool {
        args.get("run_id")
            .is_none_or(|id| id.is_null() || id == &self.id)
    }
}
struct Server {
    initialized: bool,
    received_initialize: bool,
    form: bool,
    provider: Option<Provider>,
    mode: String,
    run: Option<Run>,
    events: mpsc::Sender<Event>,
}
impl Server {
    fn new(mode: &str, events: mpsc::Sender<Event>) -> Self {
        Self {
            initialized: false,
            received_initialize: false,
            form: false,
            provider: Provider::from_production_environment().ok(),
            mode: mode.to_owned(),
            run: None,
            events,
        }
    }
    fn message(&mut self, message: &Value) -> Option<Value> {
        if message["jsonrpc"] != "2.0" {
            return Some(protocol_error(
                &Value::Null,
                -32600,
                "Invalid JSON-RPC message.",
            ));
        }
        let Some(method) = message["method"].as_str() else {
            return self.form_response(message);
        };
        let args = &message["params"];
        let Some(id) = message.get("id") else {
            if method == "notifications/initialized" && self.received_initialize {
                self.initialized = true;
            }
            if method == "notifications/cancelled" {
                let cancel = self
                    .run
                    .as_ref()
                    .and_then(|run| run.pending.as_ref())
                    .and_then(|p| p.presentation.as_ref())
                    .is_some_and(|(call, _)| args.get("requestId") == Some(call));
                if cancel {
                    self.cancel("cancelled");
                }
            }
            return None;
        };
        if !id.is_string() && !id.is_i64() && !id.is_u64() {
            return Some(protocol_error(&Value::Null, -32600, "Invalid request ID."));
        }
        if method == "initialize" {
            if self.received_initialize {
                return Some(protocol_error(id, -32600, "Already initialized."));
            }
            if !args["protocolVersion"].is_string() {
                return Some(protocol_error(id, -32602, "Missing protocol version."));
            }
            self.received_initialize = true;
            self.form = args["capabilities"]["elicitation"]
                .as_object()
                .is_some_and(|caps| {
                    caps.is_empty() || caps.get("form").is_some_and(Value::is_object)
                });
            return Some(reply(
                id,
                json!({"protocolVersion":"2025-11-25","capabilities":{"tools":{}},"serverInfo":{"name":"decision-browser","version":env!("CARGO_PKG_VERSION")}}),
            ));
        }
        if method == "ping" {
            return Some(reply(id, json!({})));
        }
        if !self.initialized {
            return Some(protocol_error(id, -32002, "Session not initialized."));
        }
        if method == "tools/list" {
            return Some(reply(id, json!({"tools":tool_contracts::tools()})));
        }
        if method != "tools/call" {
            return Some(protocol_error(id, -32601, "Method not found."));
        }
        let name = args["name"].as_str().unwrap_or("");
        let default_args = json!({});
        let params = args.get("arguments").unwrap_or(&default_args);
        if tool_contracts::validate(name, params).is_err() {
            return Some(protocol_error(id, -32602, "Invalid tool arguments."));
        }
        if name == "capabilities" {
            return Some(reply(id, self.capabilities()));
        }
        if name == "start_task" {
            return Some(reply(id, self.start(&params["request"])));
        }
        let Some(run) = self.run.as_ref() else {
            return Some(reply(id, domain("unknown")));
        };
        if !run.matches(params) {
            return Some(reply(id, domain("unknown")));
        }
        match name {
            "get_task" => Some(reply(id, run.snapshot())),
            "cancel_task" => {
                self.cancel("cancelled");
                Some(reply(id, self.run.as_ref().expect("run").snapshot()))
            }
            "read_artifact" => Some(reply(id, self.artifact(params))),
            "present_admission" => self.present(id, params),
            _ => Some(reply(id, domain("unknown"))),
        }
    }
    fn capabilities(&self) -> Value {
        let mut facts = json!({"modes":if self.mode=="task_scope" {json!(["isolated"])}else{json!(["isolated","existing_chrome"])},"ops":["scroll","wait","navigate","click","fill","select","enter","submit"],"classes":["fixture_mapped","observe_driver","ordinary_admitted","ordinary_task_scoped","ordinary_beta_admitted","consequential","unresolved","owned_loopback_document"],"artifact_read":{"format":"decision-browser-artifact-range-v1","max_chunk_bytes":RANGE,"default_channel":"legacy","channels":["legacy","structured","resource"]},"deferred_consequences":true,"native_human":"NOT_RUN","live_ac10":"NOT_RUN","both_modes":"NOT_RUN","production_ready":false,"admission_mode":self.mode,"human_grant_channel":if self.mode=="standard_form"{json!("standard_mcp_form")}else{Value::Null},"form_candidate_unverified":self.mode=="standard_form"});
        let status = crate::provider::production_provider_status();
        if let Some(status) = status.as_object() {
            for (key, value) in status {
                facts[key] = value.clone();
            }
        }
        facts["live_status"] = json!(if facts["provider_ready"] == true {
            "PREREQUISITES_PRESENT"
        } else {
            "BLOCKED"
        });
        body(facts, false)
    }
    fn start(&mut self, request: &Value) -> Value {
        if self.mode == "task_scope" && request["mode"] == "existing_chrome" {
            return domain("task_scope_isolated_only");
        }
        if self.run.is_some() {
            return domain("busy");
        }
        let Some(provider) = self.provider else {
            return domain("provider_unavailable");
        };
        let id = new_id("run");
        let gate = Arc::new(Gate {
            cancelled: AtomicBool::new(false),
            answer: Mutex::new(None),
            ready: Condvar::new(),
            events: self.events.clone(),
            deadline: Instant::now()
                + Duration::from_secs_f64(
                    request["limits"]["max_seconds"].as_f64().unwrap_or(120.0),
                ),
        });
        let request = request.clone();
        let result = json!({"run_id":id,"terminal":"queued","functional":"not_run","visual_diagnostic":if request["visual"]==true{"not_run"}else{"not_requested"},"usage":{"calls":0,"input_bytes":0,"output_bytes":0,"steps":0,"stored_bytes":0},"timings":{},"artifact_ids":[],"reason":"preparing_admission","recovery":null,"admission_class":"unresolved","effect_guarantee":"none","development_only":false,"last_url":null,"evidence":[]});
        let blocked = request["privacy"]["blocked_literals"]
            .as_array()
            .map(|l| {
                l.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let max_stored = request["limits"]["max_stored_bytes"]
            .as_u64()
            .unwrap_or(10485760) as usize;
        let worker_gate = gate.clone();
        let events = self.events.clone();
        let worker = thread::spawn(move || {
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                task_runner::execute(&request, provider, worker_gate.as_ref())
            })).unwrap_or_else(|_| TaskOutcome {
                terminal:"outcome_unknown".to_owned(), functional:"failed".to_owned(), visual_diagnostic:if request["visual"]==true {"not_run".to_owned()} else {"not_requested".to_owned()},
                reason:Some("cleanup_unknown".to_owned()), last_url:None,
                usage:json!({"calls":0,"steps":0,"input_bytes":0,"output_bytes":0,"stored_bytes":0}),
                timings:json!({}), evidence:Vec::new(), artifacts:Vec::new()
            });
            let _ = events.send(Event::Finished(outcome));
        });
        self.run = Some(Run {
            id,
            result,
            pending: None,
            gate,
            worker: Some(worker),
            completed: None,
            artifacts: BTreeMap::new(),
            blocked,
            max_stored,
            admission_denial: None,
            admitted_origin: None,
        });
        self.run.as_ref().expect("run").snapshot()
    }
    fn proposal(&mut self, proposal: Value) {
        let Some(run) = self.run.as_mut() else {
            return;
        };
        if run.gate.cancelled() {
            run.gate.answer(Err("cancelled"));
            return;
        }
        if self.mode == "task_scope" {
            let navigation = proposal["kind"] == "alpha";
            if navigation {
                let Some(origin) = proposal["origin"].as_str() else {
                    run.gate.answer(Err("task_scope_expansion_required"));
                    return;
                };
                if run
                    .admitted_origin
                    .as_deref()
                    .is_some_and(|admitted| admitted != origin)
                {
                    run.gate.answer(Err("task_scope_expansion_required"));
                    return;
                }
                run.admitted_origin = Some(origin.to_owned());
            }
            let ordinary = proposal["classification"] == "ordinary"
                && run.admitted_origin.as_deref() == proposal["document_origin"].as_str()
                && run.admitted_origin.as_deref() == proposal["destination_origin"].as_str();
            if !(navigation || ordinary) {
                run.gate.answer(Err("task_scope_expansion_required"));
                return;
            }
            run.result["admission_class"] = json!("ordinary_task_scoped");
            run.result["terminal"] = json!("running");
            run.result["reason"] = Value::Null;
            run.gate.answer(Ok(()));
            return;
        }
        let mut scope = if proposal.is_object() {
            proposal
        } else {
            json!({})
        };
        scope["run_id"] = json!(run.id);
        scope["proposal_id"] = json!(new_id("proposal"));
        scope["expires_at"] = json!(
            seconds()
                + FORM_TTL
                    .min(run.gate.deadline.saturating_duration_since(Instant::now()))
                    .as_secs_f64()
        );
        scope["deadline"] = scope["expires_at"].clone();
        scope["development_only"] = json!(false);
        scope.as_object_mut().expect("scope").remove("digest");
        scope["digest"] = json!(sha(scope.to_string().as_bytes()));
        run.result["terminal"] = json!("awaiting_human_admission");
        run.result["reason"] = json!("awaiting_human_admission");
        run.pending = Some(Pending {
            scope,
            expires: (Instant::now() + FORM_TTL).min(run.gate.deadline),
            presentation: None,
        });
    }
    fn present(&mut self, id: &Value, args: &Value) -> Option<Value> {
        let run = self.run.as_mut().expect("run");
        let Some(pending) = run.pending.as_mut() else {
            return Some(reply(id, domain("presentation_resolved")));
        };
        if args["proposal_id"] != pending.scope["proposal_id"]
            || args["digest"] != pending.scope["digest"]
        {
            return Some(reply(id, domain("unknown")));
        }
        if pending.presentation.is_some() {
            return Some(reply(id, domain("presentation_busy")));
        }
        if !self.form {
            run.admission_denial = Some("no_supported_human_grant_channel");
            run.result["terminal"] = json!("blocked");
            run.result["reason"] = json!("no_supported_human_grant_channel");
            run.gate.answer(Err("no_supported_human_grant_channel"));
            run.pending = None;
            return Some(reply(id, domain("no_supported_human_grant_channel")));
        }
        if Instant::now() >= pending.expires {
            return Some(reply(id, domain("presentation_resolved")));
        }
        let form_id = json!(new_id("form"));
        pending.presentation = Some((id.clone(), form_id.clone()));
        Some(
            json!({"jsonrpc":"2.0","id":form_id,"method":"elicitation/create","params":{"mode":"form","message":format!("Review this browser action scope before allowing it. {}",pending.scope),"requestedSchema":{"type":"object","properties":{"reviewed":{"type":"boolean","title":"I reviewed the displayed action scope"}},"required":["reviewed"],"additionalProperties":false}}}),
        )
    }
    fn form_response(&mut self, message: &Value) -> Option<Value> {
        let run = self.run.as_mut()?;
        let pending = run.pending.as_ref()?;
        let (call, form) = pending.presentation.as_ref()?;
        if message.get("id") != Some(form) {
            return None;
        }
        let call = call.clone();
        let accepted = Instant::now() < pending.expires
            && !run.gate.cancelled()
            && message.get("error").is_none()
            && message["result"]["action"] == "accept"
            && message["result"]["content"] == json!({"reviewed":true});
        let reason = if Instant::now() >= pending.expires {
            "admission_expired"
        } else if message["result"]["action"] == "cancel" {
            "cancelled"
        } else {
            "admission_declined"
        };
        let is_beta = pending.scope["kind"] == "beta";
        run.pending = None;
        if accepted {
            run.admission_denial = None;
            run.result["admission_class"] = json!(if is_beta {
                "ordinary_beta_admitted"
            } else {
                "ordinary_admitted"
            });
            run.result["terminal"] = json!("running");
            run.result["reason"] = Value::Null;
            run.gate.answer(Ok(()));
        } else {
            run.admission_denial = Some(reason);
            run.result["terminal"] = json!(if reason == "cancelled" {
                "cancelled"
            } else {
                "blocked"
            });
            run.result["reason"] = json!(reason);
            run.gate.answer(Err(reason));
        }
        Some(reply(&call, run.snapshot()))
    }
    fn cancel(&mut self, reason: &str) {
        if let Some(run) = self.run.as_mut()
            && run.completed.is_none()
        {
            run.gate.cancel();
            if let Some(pending) = run.pending.take()
                && let Some((id, _)) = pending.presentation
            {
                let _ = self
                    .events
                    .send(Event::Outbound(reply(&id, domain("cancelled"))));
            }
            run.result["reason"] = json!(reason);
            run.result["terminal"] = json!("running");
        }
    }
    fn finished(&mut self, outcome: TaskOutcome) {
        let Some(run) = self.run.as_mut() else {
            return;
        };
        if let Some(worker) = run.worker.take() {
            let _ = worker.join();
        }
        if let Some(pending) = run.pending.take()
            && let Some((call, _)) = pending.presentation
        {
            let _ = self.events.send(Event::Outbound(reply(
                &call,
                domain("presentation_resolved"),
            )));
        }
        run.completed = Some(Instant::now());
        run.result["terminal"] = json!(outcome.terminal);
        run.result["functional"] = json!(outcome.functional);
        run.result["visual_diagnostic"] = json!(outcome.visual_diagnostic);
        run.result["reason"] = json!(outcome.reason);
        run.result["last_url"] = json!(outcome.last_url);
        run.result["usage"] = outcome.usage;
        run.result["timings"] = outcome.timings;
        run.result["evidence"] = json!(outcome.evidence);
        if let Some(reason) = run.admission_denial
            && run.result["terminal"] != "outcome_unknown"
        {
            run.result["terminal"] = json!(if reason == "cancelled" {
                "cancelled"
            } else {
                "blocked"
            });
            run.result["reason"] = json!(reason);
            run.result["functional"] =
                json!(if run.result["usage"]["steps"].as_u64().unwrap_or(0) == 0 {
                    "not_run"
                } else {
                    "failed"
                });
        }
        let mut stored = 0;
        for (media, bytes) in outcome.artifacts {
            if bytes.len() > run.max_stored.saturating_sub(stored) {
                continue;
            }
            if run.blocked.iter().any(|literal| {
                bytes
                    .windows(literal.len())
                    .any(|window| window == literal.as_bytes())
            }) {
                continue;
            }
            stored += bytes.len();
            run.artifacts
                .insert(new_id("artifact"), Artifact { bytes, media });
        }
        run.result["usage"]["stored_bytes"] = json!(stored);
        run.result["artifact_ids"] = json!(run.artifacts.keys().collect::<Vec<_>>());
    }
    fn expire(&mut self) -> Option<Value> {
        let run = self.run.as_mut()?;
        if run
            .completed
            .is_some_and(|instant| instant.elapsed() >= RETENTION)
        {
            self.run = None;
            return None;
        }
        if !run
            .pending
            .as_ref()
            .is_some_and(|p| Instant::now() >= p.expires)
        {
            return None;
        }
        let pending = run.pending.take().expect("pending");
        run.admission_denial = Some("admission_expired");
        run.gate.answer(Err("admission_expired"));
        run.result["terminal"] = json!("blocked");
        run.result["reason"] = json!("admission_expired");
        pending
            .presentation
            .map(|(call, _)| reply(&call, run.snapshot()))
    }
    fn artifact(&self, args: &Value) -> Value {
        let run = self.run.as_ref().expect("run");
        let Some(artifact) = args["artifact_id"]
            .as_str()
            .and_then(|id| run.artifacts.get(id))
        else {
            return domain("failed");
        };
        let offset = args["offset"].as_u64().unwrap_or(0) as usize;
        let limit = args["limit"].as_u64().unwrap_or(RANGE as u64) as usize;
        if offset > artifact.bytes.len() || limit == 0 || limit > RANGE {
            return domain("bad_range");
        }
        let end = offset.saturating_add(limit).min(artifact.bytes.len());
        let channel = args["channel"].as_str().unwrap_or("legacy");
        if channel == "legacy" && (offset != 0 || end < artifact.bytes.len()) {
            return domain("artifact_range_required");
        }
        let chunk = &artifact.bytes[offset..end];
        let encoded = STANDARD.encode(chunk);
        let digest = sha(&artifact.bytes);
        if channel == "legacy" {
            return body(
                json!({"id":args["artifact_id"],"media_type":artifact.media,"sha256":digest,"content":encoded}),
                false,
            );
        }
        let metadata = json!({"format":"decision-browser-artifact-range-v1","id":args["artifact_id"],"media_type":artifact.media,"sha256":digest,"chunk_sha256":sha(chunk),"total_bytes":artifact.bytes.len(),"offset":offset,"next_offset":if end==artifact.bytes.len(){Value::Null}else{json!(end)},"eof":end==artifact.bytes.len()});
        if channel == "structured" {
            let mut data = metadata.clone();
            data["content"] = json!(encoded);
            return json!({"content":[{"type":"text","text":metadata.to_string()}],"structuredContent":data});
        }
        json!({"content":[{"type":"resource","resource":{"uri":format!("decision-browser-artifact://{}/{}?offset={offset}",run.id,args["artifact_id"].as_str().unwrap_or("")),"mimeType":artifact.media,"blob":encoded}}],"structuredContent":metadata})
    }
}
fn write(stdout: &mut impl Write, value: &Value) -> io::Result<()> {
    serde_json::to_writer(&mut *stdout, value)?;
    stdout.write_all(b"\n")?;
    stdout.flush()
}
pub fn serve(admission_mode: &str) -> io::Result<()> {
    if !["standard_form", "task_scope"].contains(&admission_mode) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid admission mode",
        ));
    }
    let (sender, receiver) = mpsc::channel();
    let reader = sender.clone();
    thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            if reader.send(Event::Input(line)).is_err() {
                return;
            }
        }
        let _ = reader.send(Event::Closed);
    });
    let mut server = Server::new(admission_mode, sender);
    let mut output = io::stdout().lock();
    let mut connected = true;
    loop {
        if connected
            && let Some(response) = server.expire()
            && write(&mut output, &response).is_err()
        {
            connected = false;
            server.cancel("cancelled");
        }
        if !connected && server.run.as_ref().is_none_or(|run| run.worker.is_none()) {
            break;
        }
        let event = match receiver.recv_timeout(Duration::from_millis(20)) {
            Ok(event) => event,
            Err(mpsc::RecvTimeoutError::Timeout) => continue,
            Err(_) => break,
        };
        let response = match event {
            Event::Input(line) => match line {
                Ok(line) => match serde_json::from_str::<Value>(&line) {
                    Ok(value) => server.message(&value),
                    Err(_) => Some(protocol_error(&Value::Null, -32700, "Invalid JSON.")),
                },
                Err(_) => {
                    connected = false;
                    server.cancel("cancelled");
                    None
                }
            },
            Event::Closed => {
                connected = false;
                server.cancel("cancelled");
                None
            }
            Event::Outbound(response) => Some(response),
            Event::Proposal(scope) => {
                server.proposal(scope);
                None
            }
            Event::Finished(outcome) => {
                server.finished(outcome);
                None
            }
        };
        if connected
            && let Some(response) = response
            && write(&mut output, &response).is_err()
        {
            connected = false;
            server.cancel("cancelled");
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn server() -> Server {
        let (tx, _) = mpsc::channel();
        Server::new("standard_form", tx)
    }
    fn retained(server: &mut Server) {
        let (tx, _) = mpsc::channel();
        server.run = Some(Run {
            id: "run-test".to_owned(),
            result: json!({"run_id":"run-test","terminal":"queued","usage":{"stored_bytes":0},"admission_class":"unresolved"}),
            pending: None,
            gate: Arc::new(Gate {
                cancelled: AtomicBool::new(false),
                answer: Mutex::new(None),
                ready: Condvar::new(),
                events: tx,
                deadline: Instant::now() + FORM_TTL,
            }),
            worker: None,
            completed: None,
            artifacts: BTreeMap::new(),
            blocked: Vec::new(),
            max_stored: 10485760,
            admission_denial: None,
            admitted_origin: None,
        });
    }
    #[test]
    fn task_scope_refuses_input_without_emitting_a_form() {
        let mut server = server();
        retained(&mut server);
        server.mode = "task_scope".to_owned();
        server.proposal(json!({"kind":"beta","operation":"click"}));
        let run = server.run.as_ref().unwrap();
        assert!(run.pending.is_none());
        assert_eq!(
            *run.gate.answer.lock().unwrap(),
            Some(Err("task_scope_expansion_required"))
        );
    }
    #[test]
    fn task_scope_requires_the_recorded_origin_and_ordinary_classification() {
        let mut server = server();
        retained(&mut server);
        server.mode = "task_scope".to_owned();
        server.proposal(json!({"kind":"alpha","origin":"https://example.org"}));
        assert_eq!(
            *server.run.as_ref().unwrap().gate.answer.lock().unwrap(),
            Some(Ok(()))
        );
        server.proposal(json!({"kind":"beta","classification":"ordinary","document_origin":"https://example.org","destination_origin":"https://example.org","operation":"submit"}));
        assert_eq!(
            *server.run.as_ref().unwrap().gate.answer.lock().unwrap(),
            Some(Ok(()))
        );
        assert!(server.run.as_ref().unwrap().pending.is_none());
        for (classification, destination) in [
            ("ordinary", "https://other.example.org"),
            ("consequential", "https://example.org"),
        ] {
            server.proposal(json!({"kind":"beta","classification":classification,"document_origin":"https://example.org","destination_origin":destination,"operation":"click"}));
            assert_eq!(
                *server.run.as_ref().unwrap().gate.answer.lock().unwrap(),
                Some(Err("task_scope_expansion_required"))
            );
        }
    }
    #[test]
    fn ambiguous_input_and_cleanup_preserve_unknown_after_cancellation() {
        for reason in ["action_outcome_unknown", "cleanup_unknown"] {
            let mut server = server();
            retained(&mut server);
            server.run.as_mut().unwrap().admission_denial = Some("cancelled");
            server.finished(TaskOutcome {
                terminal: "outcome_unknown".to_owned(),
                functional: "failed".to_owned(),
                visual_diagnostic: "not_requested".to_owned(),
                reason: Some(reason.to_owned()),
                last_url: None,
                usage: json!({"steps":1,"stored_bytes":0}),
                timings: json!({}),
                evidence: Vec::new(),
                artifacts: Vec::new(),
            });
            let result = &server.run.as_ref().unwrap().result;
            assert_eq!(result["terminal"], "outcome_unknown");
            assert_eq!(result["reason"], reason);
        }
    }

    #[test]
    fn proposal_digest_and_nonce_bind_the_current_retained_run() {
        let mut server = server();
        retained(&mut server);
        server.form = true;
        server.proposal(json!({"operation":"click","destination_url":"http://127.0.0.1/"}));
        let scope = server
            .run
            .as_ref()
            .unwrap()
            .pending
            .as_ref()
            .unwrap()
            .scope
            .clone();
        let args = json!({"run_id":"run-test","proposal_id":scope["proposal_id"],"digest":scope["digest"]});
        let form = server.present(&json!(10), &args).unwrap();
        assert_eq!(form["method"], "elicitation/create");
        assert_eq!(
            server.present(&json!(11), &args).unwrap()["result"]["structuredContent"]["error"],
            "presentation_busy"
        );
        assert!(
            server
                .form_response(
                    &json!({"id":"other","result":{"action":"accept","content":{"reviewed":true}}})
                )
                .is_none()
        );
        let response=server.form_response(&json!({"id":form["id"],"result":{"action":"accept","content":{"reviewed":"true"}}})).unwrap();
        assert_eq!(
            response["result"]["structuredContent"]["result"]["terminal"],
            "blocked"
        );
        assert_eq!(
            *server.run.as_ref().unwrap().gate.answer.lock().unwrap(),
            Some(Err("admission_declined"))
        );
    }
    #[test]
    fn accepted_form_is_consumed_and_cannot_be_replayed() {
        let mut server = server();
        retained(&mut server);
        server.form = true;
        server.proposal(json!({"operations":["click"]}));
        let scope = server
            .run
            .as_ref()
            .unwrap()
            .pending
            .as_ref()
            .unwrap()
            .scope
            .clone();
        let form = server
            .present(
                &json!(10),
                &json!({"proposal_id":scope["proposal_id"],"digest":scope["digest"]}),
            )
            .unwrap();
        let response =
            json!({"id":form["id"],"result":{"action":"accept","content":{"reviewed":true}}});
        assert_eq!(
            server.form_response(&response).unwrap()["result"]["structuredContent"]["result"]["terminal"],
            "running"
        );
        assert!(server.form_response(&response).is_none());
        assert_eq!(
            *server.run.as_ref().unwrap().gate.answer.lock().unwrap(),
            Some(Ok(()))
        );
    }
    #[test]
    fn pending_approval_expires_without_a_client_reply() {
        let mut server = server();
        retained(&mut server);
        server.form = true;
        server.proposal(json!({"operations":["click"]}));
        let pending = server.run.as_mut().unwrap().pending.as_mut().unwrap();
        pending.expires = Instant::now() - Duration::from_secs(1);
        pending.presentation = Some((json!(42), json!("form-expired")));
        let result = server.expire().unwrap();
        assert_eq!(result["id"], 42);
        assert_eq!(
            result["result"]["structuredContent"]["result"]["reason"],
            "admission_expired"
        );
        assert_eq!(
            *server.run.as_ref().unwrap().gate.answer.lock().unwrap(),
            Some(Err("admission_expired"))
        );
    }
    #[test]
    fn artifact_ranges_preserve_original_bytes_and_hashes() {
        let mut server = server();
        retained(&mut server);
        let bytes = (0..70000).map(|i| (i % 251) as u8).collect::<Vec<_>>();
        server.run.as_mut().unwrap().artifacts.insert(
            "a1".to_owned(),
            Artifact {
                bytes: bytes.clone(),
                media: "application/octet-stream".to_owned(),
            },
        );
        assert_eq!(
            server.artifact(&json!({"artifact_id":"a1"}))["structuredContent"]["error"],
            "artifact_range_required"
        );
        let first = server.artifact(&json!({"artifact_id":"a1","channel":"structured"}));
        let first = &first["structuredContent"];
        assert_eq!(first["next_offset"], 65536);
        assert_eq!(first["sha256"], sha(&bytes));
        assert_eq!(
            STANDARD.decode(first["content"].as_str().unwrap()).unwrap(),
            bytes[..65536]
        );
        let last =
            server.artifact(&json!({"artifact_id":"a1","offset":65536,"channel":"resource"}));
        assert_eq!(last["structuredContent"]["eof"], true);
        assert!(last["structuredContent"].get("content").is_none());
        assert_eq!(
            STANDARD
                .decode(last["content"][0]["resource"]["blob"].as_str().unwrap())
                .unwrap(),
            bytes[65536..]
        );
        assert_eq!(
            server.artifact(&json!({"artifact_id":"a1","offset":70001,"channel":"structured"}))["structuredContent"]
                ["error"],
            "bad_range"
        );
    }
}

#[cfg(test)]
mod admission_finalization_tests {
    use super::*;

    #[test]
    fn cleanup_uncertainty_takes_precedence_over_a_recorded_denial() {
        let (events, _) = mpsc::channel();
        let mut server = Server::new("standard_form", events.clone());
        server.run = Some(Run {
            id: "run-cleanup".to_owned(),
            result: json!({"usage":{"steps":0},"reason":"admission_declined"}),
            pending: None,
            gate: Arc::new(Gate {
                cancelled: AtomicBool::new(false),
                answer: Mutex::new(None),
                ready: Condvar::new(),
                events,
                deadline: Instant::now() + FORM_TTL,
            }),
            worker: None,
            completed: None,
            artifacts: BTreeMap::new(),
            blocked: Vec::new(),
            max_stored: 1024,
            admission_denial: Some("admission_declined"),
            admitted_origin: None,
        });
        server.finished(TaskOutcome {
            terminal: "outcome_unknown".to_owned(),
            functional: "failed".to_owned(),
            visual_diagnostic: "not_requested".to_owned(),
            reason: Some("cleanup_unknown".to_owned()),
            last_url: None,
            usage: json!({"calls":0,"steps":0,"input_bytes":0,"output_bytes":0,"stored_bytes":0}),
            timings: json!({}),
            evidence: Vec::new(),
            artifacts: Vec::new(),
        });
        let result = &server.run.unwrap().result;
        assert_eq!(result["terminal"], "outcome_unknown");
        assert_eq!(result["reason"], "cleanup_unknown");
    }
}
