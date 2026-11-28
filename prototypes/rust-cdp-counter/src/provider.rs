use reqwest::blocking::Client;
use serde_json::{Value, json};
use std::error::Error;
use std::fmt;
use std::io::Read;
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

const QUESTION: &str = "action";
const CHOICES: [&str; 3] = ["increment", "reset", "stop"];
const MAX_RESPONSE_BYTES: u64 = 64 * 1024;

#[derive(Clone, Copy)]
pub enum Provider {
    Fixture,
    Jev,
    Luna,
}

pub struct DecisionEvidence {
    pub model: String,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
}

/// Error codes contain neither credentials nor provider response text.
#[derive(Debug)]
pub struct ProviderError(String);

impl fmt::Display for ProviderError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl Error for ProviderError {}

type ProviderResult<T> = Result<T, ProviderError>;

impl Provider {
    pub fn from_environment() -> ProviderResult<Self> {
        match std::env::var("DECISION_BROWSER_PROVIDER").as_deref() {
            Err(std::env::VarError::NotPresent) | Ok("fixture") => Ok(Self::Fixture),
            Ok("jev") => Ok(Self::Jev),
            Ok("luna") => Ok(Self::Luna),
            _ => Err(failure("invalid_provider")),
        }
    }

    pub fn name(self) -> &'static str {
        match self {
            Self::Fixture => "fixture",
            Self::Jev => "jev",
            Self::Luna => "luna",
        }
    }

    pub fn model(self) -> &'static str {
        match self {
            Self::Fixture => "fixture",
            Self::Jev => "jev-latest",
            Self::Luna => "gpt-6-luna",
        }
    }

    /// One model decision selects among all offered actions before native input.
    pub fn choose_increment(
        self,
        button_label: &str,
        before: u32,
    ) -> ProviderResult<DecisionEvidence> {
        if button_label != "inc" || before != 0 {
            return Err(failure("unexpected_counter_observation"));
        }
        if matches!(self, Self::Fixture) {
            return Ok(DecisionEvidence {
                model: self.model().to_owned(),
                input_tokens: None,
                output_tokens: None,
            });
        }

        let (endpoint, service) = match self {
            Self::Jev => ("https://api.typesafe.ai/v1/systemone", "TYPESAFE_API_KEY"),
            Self::Luna => ("https://api.openai.com/v1/decisions", "OPENAI_API_KEY"),
            Self::Fixture => unreachable!(),
        };
        let key = keychain_key(service)?;
        let client = Client::builder()
            .redirect(reqwest::redirect::Policy::none())
            .retry(reqwest::retry::never())
            .no_proxy()
            .connect_timeout(Duration::from_secs(10))
            .timeout(Duration::from_secs(25))
            .build()
            .map_err(|_| failure("provider_client_failed"))?;
        let response = client
            .post(endpoint)
            .bearer_auth(key)
            .json(&self.payload(button_label, before))
            .send()
            .map_err(|_| failure("provider_request_failed"))?;
        if !response.status().is_success() {
            return Err(failure(&format!(
                "provider_http_{}",
                response.status().as_u16()
            )));
        }
        if response
            .content_length()
            .is_some_and(|length| length > MAX_RESPONSE_BYTES)
        {
            return Err(failure("provider_response_too_large"));
        }
        let mut bytes = Vec::new();
        response
            .take(MAX_RESPONSE_BYTES + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| failure("provider_response_read_failed"))?;
        if bytes.len() as u64 > MAX_RESPONSE_BYTES {
            return Err(failure("provider_response_too_large"));
        }
        let response: Value = serde_json::from_slice(&bytes)
            .map_err(|_| failure("provider_response_invalid_json"))?;
        self.read_decision(&response)
    }

    fn payload(self, button_label: &str, before: u32) -> Value {
        let state = json!({
            "goal": "Increase the local counter from 0 to 1 with exactly one click.",
            "counter": before,
            "buttons": [{"label": button_label, "effect": "add 1"}, {"label": "reset", "effect": "set to 0"}]
        });
        let instructions = "Choose the next action that achieves the goal from the observed counter and buttons. Choose stop if no offered button is suitable.";
        match self {
            Self::Jev => json!({
                "model": self.model(),
                "state": state,
                "questions": {QUESTION: {
                    "type": "choice",
                    "instructions": instructions,
                    "criteria": {
                        "increment": "Click the inc button once to add 1.",
                        "reset": "Click reset to set the counter to 0.",
                        "stop": "Perform no browser input."
                    }
                }}
            }),
            Self::Luna => json!({
                "model": self.model(),
                "input": state.to_string(),
                "questions": [{
                    "type": "choice",
                    "name": QUESTION,
                    "instructions": instructions,
                    "choices": [
                        {"value": "increment", "description": "Click the inc button once to add 1."},
                        {"value": "reset", "description": "Click reset to set the counter to 0."},
                        {"value": "stop", "description": "Perform no browser input."}
                    ]
                }]
            }),
            Self::Fixture => Value::Null,
        }
    }

    fn read_decision(self, response: &Value) -> ProviderResult<DecisionEvidence> {
        let model = response["model"]
            .as_str()
            .ok_or_else(|| failure("provider_invalid_model"))?;
        let valid_model = match self {
            Self::Jev => {
                model == "jev-latest"
                    || model.strip_prefix("jev-").is_some_and(|version| {
                        !version.is_empty()
                            && version
                                .bytes()
                                .all(|byte| byte.is_ascii_digit() || byte == b'.')
                    })
            }
            Self::Luna => model == self.model(),
            Self::Fixture => false,
        };
        if !valid_model || model.len() > 64 || response.get("error").is_some() {
            return Err(failure("provider_invalid_model"));
        }
        let usage = response["usage"]
            .as_object()
            .ok_or_else(|| failure("provider_invalid_usage"))?;
        let input_tokens = usage
            .get("input_tokens")
            .and_then(Value::as_u64)
            .ok_or_else(|| failure("provider_invalid_usage"))?;
        let output_tokens = usage
            .get("output_tokens")
            .and_then(Value::as_u64)
            .ok_or_else(|| failure("provider_invalid_usage"))?;
        let answer = match self {
            Self::Jev => response["answers"]
                .as_object()
                .filter(|answers| answers.len() == 1)
                .and_then(|answers| answers.get(QUESTION)),
            Self::Luna => response["answers"]
                .as_array()
                .filter(|answers| answers.len() == 1)
                .and_then(|answers| answers.first())
                .filter(|answer| answer["name"] == QUESTION),
            Self::Fixture => None,
        }
        .ok_or_else(|| failure("provider_invalid_answers"))?;
        if answer["type"] != "choice" {
            return Err(failure("provider_invalid_answer_type"));
        }
        let choice = answer["choice"]
            .as_str()
            .filter(|choice| CHOICES.contains(choice))
            .ok_or_else(|| failure("provider_unknown_choice"))?;
        unit(&answer["confidence"])?;
        let probabilities = match self {
            Self::Jev => {
                let raw = answer["probabilities"]
                    .as_object()
                    .filter(|entries| entries.len() == CHOICES.len())
                    .ok_or_else(|| failure("provider_invalid_probabilities"))?;
                CHOICES
                    .iter()
                    .map(|value| unit(&raw[*value]))
                    .collect::<ProviderResult<Vec<_>>>()?
            }
            Self::Luna => {
                let raw = answer["probabilities"]
                    .as_array()
                    .filter(|entries| entries.len() == CHOICES.len())
                    .ok_or_else(|| failure("provider_invalid_probabilities"))?;
                CHOICES
                    .iter()
                    .map(|value| {
                        let matching = raw
                            .iter()
                            .filter(|entry| entry["value"] == *value)
                            .collect::<Vec<_>>();
                        if matching.len() != 1 {
                            return Err(failure("provider_invalid_probabilities"));
                        }
                        unit(&matching[0]["probability"])
                    })
                    .collect::<ProviderResult<Vec<_>>>()?
            }
            Self::Fixture => unreachable!(),
        };
        if matches!(self, Self::Jev) {
            let selected = CHOICES.iter().position(|value| *value == choice).unwrap();
            if (probabilities.iter().sum::<f64>() - 1.0).abs() > 1e-4
                || probabilities
                    .iter()
                    .any(|value| probabilities[selected] < *value - 1e-6)
            {
                return Err(failure("provider_invalid_probabilities"));
            }
        }
        if choice != "increment" {
            return Err(failure("provider_did_not_choose_increment"));
        }
        Ok(DecisionEvidence {
            model: model.to_owned(),
            input_tokens: Some(input_tokens),
            output_tokens: Some(output_tokens),
        })
    }
}

fn unit(value: &Value) -> ProviderResult<f64> {
    value
        .as_f64()
        .filter(|number| number.is_finite() && (0.0..=1.0).contains(number))
        .ok_or_else(|| failure("provider_invalid_probability"))
}

fn keychain_key(service: &str) -> ProviderResult<String> {
    if !cfg!(target_os = "macos") {
        return Err(failure("provider_keychain_requires_macos"));
    }
    let mut child = Command::new("/usr/bin/security")
        .args(["find-generic-password", "-s", service, "-w"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|_| failure("provider_keychain_failed"))?;
    let deadline = Instant::now() + Duration::from_secs(20);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(failure("provider_keychain_failed"));
            }
        }
    };
    if !status.success() {
        return Err(failure("provider_keychain_unavailable"));
    }
    let mut bytes = Vec::new();
    child
        .stdout
        .take()
        .ok_or_else(|| failure("provider_keychain_failed"))?
        .take(8193)
        .read_to_end(&mut bytes)
        .map_err(|_| failure("provider_keychain_failed"))?;
    let key = String::from_utf8(bytes).map_err(|_| failure("provider_keychain_invalid"))?;
    let key = key.trim_end_matches(['\r', '\n']);
    if key.is_empty() || key.len() > 8192 || !key.bytes().all(|byte| byte.is_ascii_graphic()) {
        return Err(failure("provider_keychain_invalid"));
    }
    Ok(key.to_owned())
}

fn failure(code: &str) -> ProviderError {
    ProviderError(code.to_owned())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn jev_response() -> Value {
        json!({
            "model": "jev-1.14.0",
            "usage": {"input_tokens": 123, "output_tokens": 15},
            "answers": {"action": {
                "type": "choice", "choice": "increment", "confidence": 1.0,
                "probabilities": {"increment": 1.0, "reset": 0.0, "stop": 0.0}
            }}
        })
    }

    fn luna_response() -> Value {
        json!({
            "model": "gpt-6-luna",
            "usage": {"input_tokens": 123, "output_tokens": 15},
            "answers": [{
                "type": "choice", "name": "action", "choice": "increment", "confidence": 1.0,
                "probabilities": [
                    {"value": "increment", "probability": 1.0},
                    {"value": "reset", "probability": 0.0},
                    {"value": "stop", "probability": 0.0}
                ]
            }]
        })
    }

    #[test]
    fn codecs_accept_the_offered_increment_and_usage() {
        for (provider, response) in [
            (Provider::Jev, jev_response()),
            (Provider::Luna, luna_response()),
        ] {
            let evidence = provider.read_decision(&response).unwrap();
            assert_eq!(evidence.model, response["model"].as_str().unwrap());
            assert_eq!(evidence.input_tokens, Some(123));
            assert_eq!(evidence.output_tokens, Some(15));
        }
    }

    #[test]
    fn refusals_and_unoffered_choices_cannot_unlock_input() {
        for choice in ["reset", "stop", "click_anything", ""] {
            let mut response = jev_response();
            response["answers"]["action"]["choice"] = json!(choice);
            assert!(Provider::Jev.read_decision(&response).is_err());
            let mut response = luna_response();
            response["answers"][0]["choice"] = json!(choice);
            assert!(Provider::Luna.read_decision(&response).is_err());
        }
    }

    #[test]
    fn incomplete_or_invalid_distribution_is_rejected() {
        let mut response = jev_response();
        response["answers"]["action"]["probabilities"] = json!({"increment": 1.0});
        assert!(Provider::Jev.read_decision(&response).is_err());
        let mut response = luna_response();
        response["answers"][0]["probabilities"][1] =
            json!({"value": "increment", "probability": 0});
        assert!(Provider::Luna.read_decision(&response).is_err());
        let mut response = jev_response();
        response["answers"]["action"]["confidence"] = json!(true);
        assert!(Provider::Jev.read_decision(&response).is_err());
    }

    #[test]
    fn unknown_model_is_not_copied_into_evidence() {
        for model in ["arbitrary provider text", "jev-", "jev-SECRET", ""] {
            let mut response = jev_response();
            response["model"] = json!(model);
            assert!(Provider::Jev.read_decision(&response).is_err());
        }
    }

    #[test]
    fn invalid_observation_cannot_trigger_a_request() {
        for provider in [Provider::Fixture, Provider::Jev, Provider::Luna] {
            assert!(provider.choose_increment("reset", 0).is_err());
            assert!(provider.choose_increment("inc", 1).is_err());
        }
    }
}
